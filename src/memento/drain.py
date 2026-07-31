"""The deferred write runner (ADR D3.2).

Session exit closes the journal and enqueues. Nothing else — no LLM call, no git, no store write.
The consolidation happens later, in a **detached subprocess**.

A subprocess, not a thread, and not for tidiness: the abort handler installs POSIX signal handlers,
which only work on the main thread, so the operator-visible "Ctrl-C twice to abort, keep the journal"
contract survives intact *inside the child*.

Placement is **spawn-gated by the parent**, because a detached child cannot observe parent state and
cannot pause a 19-second model call mid-flight. So the rule is about when to *start*, never about
suspending:

* only after the session's read prefix is fully materialized — a drain rewriting the documents while
  the prefix reader concatenates them yields a torn composite, and one-session-*stale* is fine where
  *inconsistent* is not;
* only after N seconds of session idle.

Once spawned, the drain runs to completion. Spawn-gating **reduces overlap, it does not eliminate
it** — speech resuming right after the spawn still overlaps the call. That residual is empirical and
guarded downstream, not hidden here.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Protocol

from .adapter import Adapter
from .clock import DEFAULT_CLOCK, Clock
from .errors import ClaimHeld, DrainRefused, GateFailure, SecretsDetected, StaleProposal
from .flags import (
    BACKLOG,
    BACKUP_FAILED,
    LLM_UNAVAILABLE,
    STALE_CLAIM,
    WRITE_FAILED,
    FlagSink,
)
from .gates import Proposal, StoreState
from .locking import DEFAULT_CLAIM_TTL, SessionClaim, StoreLock, stale_claims
from .queue import Queue
from .store import MemoryStore
from .writepath import apply_consolidation, current_state, facts_fingerprint

DEFAULT_MIN_IDLE_SECONDS = 5.0


class Distiller(Protocol):
    """The LLM call, injected — which is exactly what makes write discipline testable without one."""

    def distill(self, journal: list[dict[str, Any]], state: StoreState, prompt: str) -> Proposal: ...


@dataclass(frozen=True)
class DrainGate:
    """The parent's decision to start a drain. Both conditions, or no spawn."""

    prefix_materialized: bool
    idle_seconds: float
    min_idle_seconds: float = DEFAULT_MIN_IDLE_SECONDS

    def refusal(self) -> str | None:
        if not self.prefix_materialized:
            return "read prefix is not materialized yet; a drain now could tear the composite read"
        if self.idle_seconds < self.min_idle_seconds:
            return (
                f"session has been idle {self.idle_seconds:.1f}s, "
                f"below the {self.min_idle_seconds:.1f}s gate"
            )
        return None

    def allows(self) -> bool:
        return self.refusal() is None


@dataclass
class DrainReport:
    consolidated: list[str] = field(default_factory=list)
    deferred: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    committed: list[str] = field(default_factory=list)
    flags: list[Any] = field(default_factory=list)


def spawn_drain(
    *,
    store_root: str | os.PathLike[str],
    queue_root: str | os.PathLike[str],
    adapter_ref: str,
    distiller_ref: str,
    gate: DrainGate,
    max_sessions: int | None = None,
    python: str | None = None,
) -> subprocess.Popen[bytes]:
    """Start the detached drain child. Refuses outside the gate rather than starting anyway."""
    refusal = gate.refusal()
    if refusal is not None:
        raise DrainRefused(refusal)

    argv = [
        python or sys.executable,
        "-m",
        "memento.drain",
        "--store",
        str(store_root),
        "--queue",
        str(queue_root),
        "--adapter",
        adapter_ref,
        "--distiller",
        distiller_ref,
    ]
    if max_sessions is not None:
        argv += ["--max-sessions", str(max_sessions)]

    devnull = subprocess.DEVNULL
    return subprocess.Popen(
        argv,
        stdin=devnull,
        stdout=devnull,
        stderr=devnull,
        start_new_session=True,  # detached: it outlives the parent session by design
        close_fds=True,
    )


def backlog_flag(
    queue: Queue,
    sink: FlagSink,
    *,
    max_pending: int = 5,
    max_age_days: float = 7.0,
) -> None:
    """Surface deferral rot at session start. Deferred must never mean forgotten."""
    status = queue.backlog(max_pending=max_pending, max_age_days=max_age_days)
    message = status.message()
    if message:
        sink.raise_flag(BACKLOG, message)


def run_drain(
    store: MemoryStore,
    adapter: Adapter,
    distiller: Distiller,
    queue: Queue,
    *,
    sink: FlagSink | None = None,
    lock: StoreLock | None = None,
    max_sessions: int | None = None,
    clock: Clock = DEFAULT_CLOCK,
    claim_ttl: float = DEFAULT_CLAIM_TTL,
    do_push: bool = True,
) -> DrainReport:
    """Drain pending sessions. Safe to run concurrently with another drain on the same store."""
    from . import backup as backup_mod

    sink = sink if sink is not None else FlagSink()
    lock = lock or StoreLock.for_store(store.locks_dir)
    report = DrainReport()

    for info in stale_claims(store.locks_dir, now=clock.now(), ttl=claim_ttl):
        sink.raise_flag(
            STALE_CLAIM,
            f"claim held since {info.acquired_at:.0f} by pid {info.pid}",
            session=info.session,
        )

    pending = queue.pending_sessions()
    if max_sessions is not None:
        pending = pending[:max_sessions]

    for item in pending:
        session = item.session
        claim = SessionClaim(store.locks_dir, session, clock=clock)
        try:
            # Claim acquisition is a queue operation, so it runs under the store lock. The lock is
            # released the moment the claim is in hand — long before the model call below.
            with lock.hold():
                claim.acquire()
        except ClaimHeld:
            report.skipped.append(session)
            continue

        try:
            if queue.is_consolidated(session):
                report.skipped.append(session)
                continue

            journal = queue.read_journal(session)
            state = current_state(store, adapter)
            fingerprint = facts_fingerprint(state.facts)

            # --- no lock is held across this call, by construction ---
            try:
                proposal = distiller.distill(journal, state, adapter.distillation_prompt)
            except Exception as exc:  # LLM unavailable, malformed output, anything
                reason = f"{type(exc).__name__}: {exc}"
                queue.mark_deferred(session, reason)
                sink.raise_flag(LLM_UNAVAILABLE, reason, session=session)
                report.deferred.append((session, reason))
                continue

            # Post-call backstop: another front-end may have finished this session while the model
            # was thinking. Re-check so two front-ends never both pay for one consolidation.
            if queue.is_consolidated(session):
                report.skipped.append(session)
                continue

            try:
                apply_consolidation(
                    store,
                    adapter,
                    proposal,
                    session=session,
                    batch=f"drain-{session}",
                    lock=lock,
                    queue=queue,
                    sink=sink,
                    expected_fingerprint=fingerprint,
                )
            except (GateFailure, SecretsDetected, StaleProposal) as exc:
                report.deferred.append((session, str(exc)))  # already deferred and flagged
                continue
            except Exception as exc:
                # One bad session must not take the drain down with it. Deferral is bounded and
                # visible (D3.5/D3.6); an escaping exception would strand every session behind it.
                reason = f"{type(exc).__name__}: {exc}"
                queue.mark_deferred(session, reason)
                sink.raise_flag(WRITE_FAILED, reason, session=session)
                report.deferred.append((session, reason))
                continue

            if backup_mod.is_enabled(store):
                # The whole of backup is best-effort. A failed commit must not strand a session
                # that is already written, nor the sessions queued behind it — the store is the
                # record, git is a copy of it.
                try:
                    sha = backup_mod.commit_consolidation(store, session, lock=lock)
                    if sha:
                        report.committed.append(sha)
                except Exception as exc:
                    sink.raise_flag(BACKUP_FAILED, f"backup commit failed: {exc}", session=session)
                if do_push:
                    try:
                        backup_mod.push(store)  # outside the lock, on purpose
                    except Exception as exc:  # a remote being down must never lose a local write
                        sink.raise_flag(
                            BACKUP_FAILED, f"backup push failed: {exc}", session=session
                        )

            queue.mark_consolidated(session)  # marker LAST, always
            report.consolidated.append(session)
        finally:
            claim.release()

    report.flags = list(sink.flags)
    return report


# ------------------------------------------------------------------- child


def _resolve(ref: str) -> Any:
    """Resolve a `module:attribute` reference. A class is instantiated; anything else is used as-is."""
    import importlib

    module_name, _, attr = ref.partition(":")
    if not attr:
        raise SystemExit(f"expected 'module:attribute', got {ref!r}")
    obj = getattr(importlib.import_module(module_name), attr)
    return obj() if isinstance(obj, type) else obj


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memento.drain", description="Run a memory drain.")
    parser.add_argument("--store", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--adapter", required=True, help="module:attribute")
    parser.add_argument("--distiller", required=True, help="module:attribute")
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args(argv)

    adapter = _resolve(args.adapter)
    distiller = _resolve(args.distiller)
    store = MemoryStore(args.store)
    queue = Queue(args.queue, retention=adapter.retention)
    report = run_drain(
        store,
        adapter,
        distiller,
        queue,
        max_sessions=args.max_sessions,
        do_push=not args.no_push,
    )
    return 0 if not report.deferred else 1


if __name__ == "__main__":  # pragma: no cover - subprocess entrypoint
    raise SystemExit(main())
