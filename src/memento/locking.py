"""Locks and claims (ADR D3.3).

Two distinct primitives, deliberately not the same thing:

* **Store lock** — one per `store_root`. Serializes consolidation writes, queue claim operations,
  and the local half of autocommit. It is *never* held across the LLM call and *never* across
  `pull`/`push`, so its maximum hold is bounded by local I/O.
* **Session claim** — one per session, ephemeral. Held for the claiming process's lifetime via
  `flock`, so the OS releases it if the holder dies and a later drain reclaims the session. PID and
  timestamp are recorded alongside for the staleness-TTL variant and for operator visibility.

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
from .errors import ClaimHeld, LockTimeout
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


class StoreLock:
    """The one write lock for a store.

    Re-entrant **per thread** and mutually exclusive **between** threads, which needs an in-process
    `RLock` as well as the `flock`: `flock` is per open file description, so two handles in one
    process do exclude each other, but a plain depth counter shared by two threads does not. Sharing
    one counter let both threads inside the critical section, underflowed it to -1, and — since -1
    is truthy — turned the lock into a permanent no-op for the life of the process.

    Use `for_store()` rather than constructing one per call site: two different handles on the same
    store deadlock each other for the full timeout, which is what an operator `forget` inside a held
    lock used to do.
    """

    _instances: "dict[Path, StoreLock]" = {}
    _registry_guard = threading.RLock()

    def __new__(cls, locks_dir: Path, *, timeout: float = DEFAULT_LOCK_TIMEOUT) -> "StoreLock":
        """One instance per store, however many times a caller constructs it.

        Two *different* handles on the same store do not compose — they block each other on the
        `flock` until the timeout expires, which is what an operator `forget` inside a held lock ran
        into. Canonicalising here means callers cannot reintroduce that by constructing their own.
        """
        key = Path(locks_dir).resolve()
        with cls._registry_guard:
            existing = cls._instances.get(key)
            if existing is not None:
                return existing
            self = super().__new__(cls)
            self.path = key / "store.lock"
            self.timeout = timeout
            self._reentrant = threading.RLock()
            self._depth = 0
            self._fd = None
            cls._instances[key] = self
            return self

    def __init__(self, locks_dir: Path, *, timeout: float = DEFAULT_LOCK_TIMEOUT) -> None:
        # State is set once, in __new__. A second construction must not reset the depth of a lock
        # another frame is currently holding.
        pass

    @classmethod
    def for_store(cls, locks_dir: Path, *, timeout: float = DEFAULT_LOCK_TIMEOUT) -> "StoreLock":
        """Explicit spelling of the same canonicalisation. Preferred at call sites for clarity."""
        return cls(locks_dir, timeout=timeout)

    @contextmanager
    def hold(self, *, timeout: float | None = None) -> Iterator[None]:
        wait = self.timeout if timeout is None else timeout
        if not self._reentrant.acquire(timeout=wait):
            raise LockTimeout(f"another thread holds {self.path} (waited {wait}s)")
        try:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            with file_lock(self.path, timeout=wait) as fd:
                self._depth = 1
                self._fd = fd
                try:
                    yield
                finally:
                    self._depth = 0
                    self._fd = None
        finally:
            self._reentrant.release()

    @property
    def held(self) -> bool:
        return self._depth > 0


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
        claim = SessionClaim(directory, session)
        info = claim.read_info()
        if info is not None and info.is_stale(now, ttl):
            out.append(info)
    return out
