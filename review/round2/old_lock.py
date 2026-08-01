"""Verbatim pre-fix StoreLock (from git show d8ab7ca:src/memento/locking.py)."""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from memento.locking import file_lock, DEFAULT_LOCK_TIMEOUT

class OldStoreLock:
    def __init__(self, locks_dir, *, timeout=DEFAULT_LOCK_TIMEOUT):
        self.path = Path(locks_dir) / "store.lock"
        self.timeout = timeout
        self._depth = 0
        self._fd = None

    @contextmanager
    def hold(self, *, timeout=None):
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        with file_lock(self.path, timeout=self.timeout if timeout is None else timeout) as fd:
            self._depth = 1
            self._fd = fd
            try:
                yield
            finally:
                self._depth = 0
                self._fd = None

    @property
    def held(self):
        return self._depth > 0
