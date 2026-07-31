"""Append-only JSONL event logs (ADR D2).

One file per stream. Every event is stamped `ts / session / turn / batch / ordinal`. Batch appends
are idempotent: replaying a batch after a crash appends nothing.

Wire format is flat — envelope keys and payload keys share one JSON object, which is what the jubs
store already looks like on disk. `turn` is omitted when unset so pre-existing logs round-trip byte-
identically after a read/write cycle.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .clock import DEFAULT_CLOCK, Clock
from .errors import CorruptStoreError, MementoError

ENVELOPE_KEYS = ("id", "event", "ts", "session", "turn", "batch", "ordinal")

# Stand-in stamp for events written before the engine existed and missing envelope fields.
LEGACY = "legacy"

# Event kinds the engine itself understands. Adapters add their own freely; the fold only gives
# special meaning to these.
EVENT_ENTRY = "entry"
EVENT_SUPERSEDED_BY = "superseded_by"
EVENT_RETIRED = "retired"
EVENT_CONTRADICTED = "contradicted"
EVENT_DOCUMENT_REPLACED = "document_replaced"


@dataclass(frozen=True)
class Event:
    event: str
    session: str
    batch: str
    ordinal: int
    ts: str
    id: str | None = None
    turn: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_obj(self) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        if self.id is not None:
            obj["id"] = self.id
        obj["event"] = self.event
        obj["ts"] = self.ts
        obj["session"] = self.session
        if self.turn is not None:
            obj["turn"] = self.turn
        obj["batch"] = self.batch
        obj["ordinal"] = self.ordinal
        for k, v in self.payload.items():
            if k in ENVELOPE_KEYS:
                raise ValueError(f"payload key {k!r} collides with the event envelope")
            obj[k] = v
        return obj

    @classmethod
    def from_obj(cls, obj: Mapping[str, Any], *, position: int = 0) -> "Event":
        """Parse one on-disk event.

        Deliberately permissive about a missing envelope field: a log written before the engine
        existed is the compatibility target, and refusing to read it would turn adoption into a
        migration. Writes are strict; reads meet the store where it is.
        """
        payload = {k: v for k, v in obj.items() if k not in ENVELOPE_KEYS}
        try:
            ordinal = int(obj.get("ordinal", position))
        except (TypeError, ValueError):
            ordinal = position
        return cls(
            event=str(obj.get("event", EVENT_ENTRY)),
            session=str(obj.get("session", LEGACY)),
            batch=str(obj.get("batch", LEGACY)),
            ordinal=ordinal,
            ts=str(obj.get("ts", "")),
            id=obj.get("id"),
            turn=obj.get("turn"),
            payload=payload,
        )

    def same_content_as(self, other: "Event") -> bool:
        """Identity for replay purposes. `ts` is excluded — a re-run legitimately restamps."""
        return (
            self.event == other.event
            and self.id == other.id
            and self.ordinal == other.ordinal
            and self.turn == other.turn
            and dict(self.payload) == dict(other.payload)
        )

    @property
    def batch_key(self) -> tuple[str, str]:
        return (self.session, self.batch)


class EventLog:
    """One append-only JSONL file.

    Reads tolerate a torn *trailing* line — the only partial write a crash mid-append can leave —
    and refuse anything else, because a corrupt line in the middle means something rewrote history.
    """

    def __init__(self, path: Path, clock: Clock = DEFAULT_CLOCK) -> None:
        self.path = Path(path)
        self.clock = clock

    def exists(self) -> bool:
        return self.path.exists()

    def _decode(self) -> str:
        raw = self.path.read_bytes()
        try:
            # utf-8-sig: a BOM on a pre-existing log is a quirk of whatever wrote it, not corruption.
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CorruptStoreError(f"{self.path}: not valid UTF-8 at byte {exc.start}") from exc

    def read(self) -> list[Event]:
        if not self.path.exists():
            return []
        lines = self._decode().split("\n")
        # A trailing newline yields a final empty element; that is a clean file, not a torn one.
        trailing_partial = bool(lines and lines[-1] != "")
        events: list[Event] = []
        for i, line in enumerate(lines):
            line = line.rstrip("\r")
            if line == "":
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                if trailing_partial and i == len(lines) - 1:
                    break  # torn tail from a crash mid-append: ignore, the batch will be replayed
                raise CorruptStoreError(f"{self.path}: unparseable line {i + 1}") from exc
            events.append(Event.from_obj(obj, position=len(events)))
        return events

    def has_batch(self, session: str, batch: str) -> bool:
        return any(e.batch_key == (session, batch) for e in self.read())

    def _repair_tail(self) -> None:
        """Make the file safe to append to.

        Two ways a log can lack a final newline, and they need opposite treatment:

        * a **torn** tail — a crash mid-append left half an event. Truncate it; the batch that wrote
          it was never acknowledged and will be replayed.
        * a **complete** last event that simply has no trailing newline — a pre-engine log. Keep it
          and add the newline.

        Skipping this is how an append welds two events into one unparseable line and kills the
        stream for good, which is worse than either thing it was recovering from.
        """
        if not self.path.exists():
            return
        raw = self.path.read_bytes()
        if not raw or raw.endswith(b"\n"):
            return
        cut = raw.rfind(b"\n") + 1  # 0 when the file is a single line
        tail = raw[cut:]
        try:
            json.loads(tail.decode("utf-8-sig").rstrip("\r"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            keep = raw[:cut]  # torn: drop it
        else:
            keep = raw + b"\n"  # complete: terminate it
        with open(self.path, "r+b") as fh:
            fh.seek(0)
            fh.write(keep)
            fh.truncate()
            fh.flush()
            os.fsync(fh.fileno())

    def append_batch(
        self,
        entries: Iterable[Mapping[str, Any] | Event],
        *,
        session: str,
        batch: str,
        turn: int | None = None,
        ts: str | None = None,
    ) -> list[Event]:
        """Append a whole batch, or nothing.

        Idempotent on `(session, batch)`: replaying a batch already on disk returns it and touches
        nothing. Ordinals are assigned by position within the batch.

        Re-using a key for *different* entries is a caller bug, not a replay, and it raises — the
        alternative is dropping the new entries and reporting success, which is how a consolidation
        silently loses half of itself.
        """
        stamp = ts or self.clock.now_iso()
        events: list[Event] = []
        for ordinal, entry in enumerate(entries):
            if isinstance(entry, Event):
                events.append(
                    Event(
                        event=entry.event,
                        session=session,
                        batch=batch,
                        ordinal=ordinal,
                        ts=entry.ts or stamp,
                        id=entry.id,
                        turn=entry.turn if entry.turn is not None else turn,
                        payload=entry.payload,
                    )
                )
                continue
            data = dict(entry)
            kind = data.pop("event", EVENT_ENTRY)
            eid = data.pop("id", None)
            events.append(
                Event(
                    event=kind,
                    session=session,
                    batch=batch,
                    ordinal=ordinal,
                    ts=data.pop("ts", stamp),
                    id=eid,
                    turn=data.pop("turn", turn),
                    payload=data,
                )
            )
        if not events:
            return []

        already = [e for e in self.read() if e.batch_key == (session, batch)]
        if already:
            if len(already) != len(events) or not all(
                a.same_content_as(b) for a, b in zip(already, events)
            ):
                raise MementoError(
                    f"{self.path}: batch ({session!r}, {batch!r}) is already recorded with "
                    "different entries; use a new batch id rather than re-using this one"
                )
            return already

        blob = "".join(json.dumps(e.to_obj(), ensure_ascii=False) + "\n" for e in events)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._repair_tail()
        # One write, one fsync: the whole batch reaches the page cache as a single call, so the only
        # crash residue possible is a torn tail, which read() already tolerates and this replays.
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        return events
