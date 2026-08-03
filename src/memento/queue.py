"""The queue — the store's second, explicitly unversioned area (ADR D2/D8).

Journals, the pending log, and the `consolidated` markers live here. This is where the verbatim
pile accumulates (per-turn transcript material), which is why retention is an adapter policy the
consumer must state out loud rather than a default the engine picks.

    <queue_root>/
      pending.jsonl                append-only: enqueued / deferred records
      <session>/journal.jsonl      per-turn material
      <session>/consolidated       marker — written LAST, always

**Marker-LAST is the crash-safety invariant**: a crash before the marker means the session is still
pending and gets re-run, which is why nothing else may stand in for it. Claims are a separate,
ephemeral artifact and live in the store's lock directory (see `locking`).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .clock import DEFAULT_CLOCK, Clock
from .errors import MementoError
from .ids import validate_session_id

PENDING_LOG = "pending.jsonl"
JOURNAL = "journal.jsonl"
CONSOLIDATED_MARKER = "consolidated"

REC_ENQUEUED = "enqueued"
REC_DEFERRED = "deferred"


@dataclass(frozen=True)
class RetentionPolicy:
    """What happens to a session's transcript material once it has been consolidated.

    `keep_everything` is a perfectly good policy. "Not persisted in the memory
    store" is never to be read as "transient" — the material sits here until a policy says otherwise.
    """

    keep_everything: bool = True
    prune_after_consolidation: bool = False

    def __post_init__(self) -> None:
        if self.keep_everything and self.prune_after_consolidation:
            raise ValueError("retention policy cannot both keep everything and prune")


@dataclass(frozen=True)
class BacklogStatus:
    pending: int
    oldest_age_days: float
    breached: bool
    reason: str | None

    def message(self) -> str | None:
        if not self.breached:
            return None
        return f"memory is stale by {self.pending} session(s): {self.reason}"


@dataclass(frozen=True)
class PendingSession:
    session: str
    enqueued_at: float
    deferrals: int


class Queue:
    def __init__(
        self,
        queue_root: str | os.PathLike[str],
        *,
        clock: Clock = DEFAULT_CLOCK,
        retention: RetentionPolicy = RetentionPolicy(),
    ) -> None:
        self.root = Path(queue_root).resolve()
        self.clock = clock
        self.retention = retention

    # ------------------------------------------------------------- journaling

    def session_dir(self, session: str) -> Path:
        """The one place a session id becomes a path — so it is the one place that validates it.

        `self.root / session` on an unchecked id writes wherever the id points: `../../x` escapes the
        queue, and an absolute id ignores the root entirely.
        """
        return self.root / validate_session_id(session)

    def append_turn(self, session: str, turn: int, payload: dict[str, Any]) -> None:
        path = self.session_dir(session) / JOURNAL
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"turn": turn, "ts": self.clock.now_iso(), **payload}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read_journal(self, session: str) -> list[dict[str, Any]]:
        path = self.session_dir(session) / JOURNAL
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # torn tail from a crash mid-turn; the rest of the journal still stands
        return out

    # ------------------------------------------------------------- enqueueing

    def close_and_enqueue(self, session: str) -> None:
        """Session exit: close the journal, record the session as pending. No git work, ever.

        This is the whole of exit — it is what keeps exit under five seconds. Everything expensive
        happens later, in the drain subprocess.
        """
        self.session_dir(session).mkdir(parents=True, exist_ok=True)
        self._append_pending({"session": session, "record": REC_ENQUEUED, "at": self.clock.now()})

    def _append_pending(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        record = {"ts": self.clock.now_iso(), **record}
        with open(self.root / PENDING_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _pending_records(self) -> Iterator[dict[str, Any]]:
        path = self.root / PENDING_LOG
        if not path.exists():
            return iter(())
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return iter(records)

    def is_enqueued(self, session: str) -> bool:
        """Whether this session was ever closed and enqueued.

        Not the same as "its directory exists": journaling a turn creates that directory too, so a
        session that was written to but never closed would otherwise look enqueued — and could be
        marked consolidated without a consolidation ever having been owed.
        """
        validate_session_id(session)
        return any(
            rec.get("session") == session and rec.get("record") == REC_ENQUEUED
            for rec in self._pending_records()
        )

    def pending_sessions(self) -> list[PendingSession]:
        """Enqueued and not yet marked consolidated, oldest first."""
        first_seen: dict[str, float] = {}
        deferrals: dict[str, int] = {}
        for rec in self._pending_records():
            session = rec.get("session")
            if not session:
                continue
            try:
                validate_session_id(session)
            except MementoError:
                continue  # a record from a build that did not validate; never act on it
            if rec.get("record") == REC_ENQUEUED:
                first_seen.setdefault(session, float(rec.get("at", 0.0)))
            elif rec.get("record") == REC_DEFERRED:
                deferrals[session] = deferrals.get(session, 0) + 1
        out = [
            PendingSession(session=s, enqueued_at=at, deferrals=deferrals.get(s, 0))
            for s, at in first_seen.items()
            if not self.is_consolidated(s)
        ]
        return sorted(out, key=lambda p: (p.enqueued_at, p.session))

    # --------------------------------------------------------------- markers

    def marker_path(self, session: str) -> Path:
        return self.session_dir(session) / CONSOLIDATED_MARKER

    def is_consolidated(self, session: str) -> bool:
        return self.marker_path(session).exists()

    def mark_consolidated(self, session: str) -> None:
        """Written LAST, after the store write has landed. Nothing may reorder this."""
        path = self.marker_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.clock.now_iso() + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if self.retention.prune_after_consolidation:
            self.prune(session)

    def mark_deferred(self, session: str, reason: str) -> None:
        validate_session_id(session)
        self._append_pending(
            {"session": session, "record": REC_DEFERRED, "reason": reason, "at": self.clock.now()}
        )

    # -------------------------------------------------------------- backlog

    def backlog(self, *, max_pending: int = 5, max_age_days: float = 7.0) -> BacklogStatus:
        pending = self.pending_sessions()
        now = self.clock.now()
        oldest = min((p.enqueued_at for p in pending), default=now)
        age_days = (now - oldest) / 86400.0 if pending else 0.0
        reason = None
        if len(pending) > max_pending:
            reason = f"{len(pending)} pending exceeds the bound of {max_pending}"
        elif age_days > max_age_days:
            reason = f"oldest pending session is {age_days:.1f} days stale"
        return BacklogStatus(
            pending=len(pending),
            oldest_age_days=age_days,
            breached=reason is not None,
            reason=reason,
        )

    # ------------------------------------------------------------- retention

    def prune(self, session: str) -> bool:
        """Drop a consolidated session's transcript material, if policy allows. Never otherwise."""
        if self.retention.keep_everything or not self.retention.prune_after_consolidation:
            return False
        if not self.is_consolidated(session):
            return False
        journal = self.session_dir(session) / JOURNAL
        if journal.exists():
            journal.unlink()
        return True
