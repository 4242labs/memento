"""Locks and claims (ADR D3.3).

Two distinct primitives, deliberately not the same thing:

* **Store lock** — one per `store_root`. Serializes consolidation writes, queue claim operations,
  and the local half of autocommit. It is *never* held across the LLM call and *never* across
  `pull`/`push`, so its maximum hold is bounded by local I/O.
* **Session claim** — one per session, ephemeral. Two implementations, for two shapes of consumer:

  * `SessionClaim` holds an `flock` for the claiming **process's lifetime**, so the OS releases it
    if the holder dies. Right for the drain, which does the whole consolidation inside one process.
  * `CasClaim` is a claim **file** with a token and a TTL, and outlives the process that took it.
    Right for an agent, whose consolidation spans several `memento` invocations with the model's own
    thinking in between — an `flock` taken by `memento claim` is gone the moment that command exits,
    so it would exclude nobody.

The claim is **not** the `consolidated` marker and never touches it. The marker stays marker-LAST —
that invariant (crash before marker ⇒ re-run, never lose) is load-bearing and nothing here goes near
it.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .clock import DEFAULT_CLOCK, Clock
from .errors import ClaimHeld, LockTimeout, MementoError
from .ids import validate_session_id

DEFAULT_LOCK_TIMEOUT = 30.0
DEFAULT_CLAIM_TTL = 3600.0
_POLL = 0.02


@contextmanager
def file_lock(path: Path, *, timeout: float = DEFAULT_LOCK_TIMEOUT) -> Iterator[int]:
    """Exclusive `flock` on `path`, polled to a timeout so a wedged holder cannot hang a session."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):  # pragma: no cover - platform
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"could not acquire {path} within {timeout}s") from exc
                time.sleep(_POLL)
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass
class _LockState:
    """The lock state for one store root, shared by every handle that points at it."""

    reentrant: threading.RLock
    owner_pid: int
    depth: int = 0
    fd: int | None = None


class StoreLock:
    """The one write lock for a store.

    Re-entrant **per thread** and mutually exclusive **between** threads and processes. Three things
    have to be true at once, and each was learned the hard way:

    * `flock` alone is not enough — a depth counter shared by two threads let both inside the
      critical section and underflowed to -1, which is truthy, so the lock became a no-op for good.
    * Two *handles* on one store must compose rather than deadlock on each other, so the reentrancy
      state lives per store path, not per object.
    * The handle itself must stay ordinary. Caching whole instances meant a second construction
      silently ignored the timeout it was given, and a forked child inherited a `depth` of 1 and
      wrote inside its parent's critical section without ever taking the flock.
    """

    _states: "dict[Path, _LockState]" = {}
    _registry_guard = threading.Lock()

    def __init__(self, locks_dir: Path, *, timeout: float = DEFAULT_LOCK_TIMEOUT) -> None:
        self.root = Path(locks_dir).resolve()
        self.path = self.root / "store.lock"
        self.timeout = timeout

    @classmethod
    def for_store(cls, locks_dir: Path, *, timeout: float = DEFAULT_LOCK_TIMEOUT) -> "StoreLock":
        """Explicit spelling for call sites; any handle on the same path shares the same state."""
        return cls(locks_dir, timeout=timeout)

    @property
    def _state(self) -> _LockState:
        pid = os.getpid()
        with self._registry_guard:
            state = self._states.get(self.root)
            if state is None or state.owner_pid != pid:
                # A fresh process — including a forked child — starts from zero. Inheriting the
                # parent's depth would let the child skip the flock entirely.
                state = _LockState(reentrant=threading.RLock(), owner_pid=pid)
                self._states[self.root] = state
            return state

    @contextmanager
    def hold(self, *, timeout: float | None = None) -> Iterator[None]:
        wait = self.timeout if timeout is None else timeout
        state = self._state
        if not state.reentrant.acquire(timeout=wait):
            raise LockTimeout(f"another thread holds {self.path} (waited {wait}s)")
        try:
            if state.depth:
                state.depth += 1
                try:
                    yield
                finally:
                    state.depth -= 1
                return
            with file_lock(self.path, timeout=wait) as fd:
                state.depth = 1
                state.fd = fd
                try:
                    yield
                finally:
                    state.depth = 0
                    state.fd = None
        finally:
            state.reentrant.release()

    @property
    def held(self) -> bool:
        return self._state.depth > 0


@dataclass
class ClaimInfo:
    session: str
    pid: int
    acquired_at: float

    def is_stale(self, now: float, ttl: float) -> bool:
        return (now - self.acquired_at) > ttl


class SessionClaim:
    """A claim on one session's consolidation, held for this process's lifetime."""

    def __init__(self, locks_dir: Path, session: str, *, clock: Clock = DEFAULT_CLOCK) -> None:
        self.locks_dir = Path(locks_dir)
        self.session = validate_session_id(session)
        self.clock = clock
        self.path = self.locks_dir / f"claim-{session}.lock"
        self._fd: int | None = None

    def acquire(self) -> "SessionClaim":
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                # A live holder. Not a failure — the caller skips this session and moves on.
                raise ClaimHeld(f"session {self.session} is claimed by another process") from exc
            raise  # pragma: no cover - platform
        # Acquiring means any previous holder is gone; stamping over it *is* the reclaim.
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        blob = json.dumps({"session": self.session, "pid": os.getpid(), "acquired_at": self.clock.now()})
        os.write(fd, blob.encode("utf-8"))
        os.fsync(fd)
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def read_info(self) -> ClaimInfo | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return None
        if "pid" not in data:
            return None
        return ClaimInfo(
            session=data.get("session", self.session),
            pid=int(data["pid"]),
            acquired_at=float(data.get("acquired_at", 0.0)),
        )

    def __enter__(self) -> "SessionClaim":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


@dataclass(frozen=True)
class CasClaimRecord:
    """What a `CasClaim` writes down. The token is the right to release it."""

    session: str
    pid: int
    token: str
    acquired_at: float
    ttl: float

    def is_stale(self, now: float) -> bool:
        return (now - self.acquired_at) > self.ttl

    def to_obj(self) -> dict[str, object]:
        return {
            "session": self.session,
            "pid": self.pid,
            "token": self.token,
            "acquired_at": self.acquired_at,
            "ttl": self.ttl,
        }


class CasClaim:
    """A session claim that survives the process that took it.

    `SessionClaim` cannot serve an agent. Its `flock` dies with the acquiring process, so a
    `memento claim` verb built on it would release on exit and exclude nothing — the loser of a race
    would go on to pay for the same consolidation, which is exactly the D2 invariant the claim
    exists to hold.

    So the claim is a **file**: acquiring writes `(pid, token, acquired_at, ttl)`, releasing requires
    the token back. Two rules keep it from becoming a wedge:

    * **Every claim expires.** A claim older than its TTL is reclaimable by anyone, so an agent that
      walked away mid-loop does not leave a session permanently unconsolidatable.
    * **Read-modify-write runs under the store lock**, which is `flock`-backed and therefore mutually
      exclusive between *processes*. An unguarded check-then-create loses the race it exists to win.

    It is not the `consolidated` marker and it never touches it.
    """

    def __init__(
        self,
        locks_dir: Path,
        session: str,
        *,
        clock: Clock = DEFAULT_CLOCK,
        ttl: float = DEFAULT_CLAIM_TTL,
        lock: StoreLock | None = None,
    ) -> None:
        self.locks_dir = Path(locks_dir)
        self.session = validate_session_id(session)
        self.clock = clock
        self.ttl = ttl
        self.path = self.locks_dir / f"claim-{self.session}.json"
        self.lock = lock or StoreLock.for_store(self.locks_dir)

    def read(self) -> CasClaimRecord | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return None  # a torn write from a crash mid-claim: unreadable is unheld
        if "token" not in data:
            return None
        return CasClaimRecord(
            session=str(data.get("session", self.session)),
            pid=int(data.get("pid", 0)),
            token=str(data["token"]),
            acquired_at=float(data.get("acquired_at", 0.0)),
            ttl=float(data.get("ttl", self.ttl)),
        )

    def acquire(self) -> CasClaimRecord:
        """Take the claim, or raise `ClaimHeld` naming who holds it and for how much longer."""
        with self.lock.hold():
            now = self.clock.now()
            existing = self.read()
            if existing is not None and not existing.is_stale(now):
                remaining = existing.ttl - (now - existing.acquired_at)
                raise ClaimHeld(
                    f"session {self.session} is claimed by pid {existing.pid} "
                    f"({remaining:.0f}s before it goes stale)"
                )
            record = CasClaimRecord(
                session=self.session,
                pid=os.getpid(),
                token=os.urandom(8).hex(),
                acquired_at=now,
                ttl=self.ttl,
            )
            self._write(record)
            return record

    def release(self, token: str) -> bool:
        """Give the claim back. Refuses a token that does not match — releasing someone else's
        claim is how two claimants end up inside the critical section together."""
        with self.lock.hold():
            existing = self.read()
            if existing is None:
                return False
            if existing.token != token:
                raise ClaimHeld(
                    f"session {self.session} is held by pid {existing.pid} under a different token"
                )
            self.path.unlink()
            return True

    def _write(self, record: CasClaimRecord) -> None:
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record.to_obj(), fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)


def cas_claims(locks_dir: Path) -> list[CasClaimRecord]:
    """Every CAS claim currently on disk, oldest first. Reporting only."""
    directory = Path(locks_dir)
    if not directory.exists():
        return []
    out = []
    for path in sorted(directory.glob("claim-*.json")):
        session = path.name[len("claim-") : -len(".json")]
        try:
            record = CasClaim(directory, session).read()
        except MementoError:
            continue  # an artifact whose session id does not validate; never act on it
        if record is not None:
            out.append(record)
    return sorted(out, key=lambda r: (r.acquired_at, r.session))


def stale_claims(
    locks_dir: Path, *, now: float, ttl: float = DEFAULT_CLAIM_TTL
) -> list[ClaimInfo]:
    """Claims whose recorded timestamp is older than the TTL.

    Reporting only — reclaim happens by acquiring, which the OS already permits once the holder is
    gone. This exists so a wedged-but-alive holder is *visible* rather than silently blocking drains.
    """
    directory = Path(locks_dir)
    if not directory.exists():
        return []
    out = []
    for path in sorted(directory.glob("claim-*.lock")):
        session = path.name[len("claim-") : -len(".lock")]
        try:
            claim = SessionClaim(directory, session)
        except MementoError:
            # An artifact from before session ids were validated. Reporting is a courtesy; letting
            # it raise here would kill every drain before it looked at a single session.
            continue
        info = claim.read_info()
        if info is not None and info.is_stale(now, ttl):
            out.append(info)
    return out
