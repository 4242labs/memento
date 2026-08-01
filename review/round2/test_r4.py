"""Round-two, part 3: regressions introduced BY the fixes."""
from __future__ import annotations
import os, subprocess
from pathlib import Path
import pytest
from memento import MemoryStore, Queue, StoreLock
from old_lock import OldStoreLock


def test_G1_the_registry_is_a_regression_across_fork(tmp_path):
    """Pre-fix, a forked child constructing StoreLock got a FRESH object with _depth == 0 and took
    the flock properly. Post-fix, __new__ hands it the inherited instance with _depth == 1, so the
    child skips the flock and writes inside the parent's critical section."""
    locks = tmp_path / "locks"; locks.mkdir()

    old = OldStoreLock(locks)
    with old.hold():
        assert OldStoreLock(locks)._depth == 0      # pre-fix: a fresh handle, correctly unheld

    new = StoreLock(locks)
    with new.hold():
        assert StoreLock(locks)._depth == 1         # post-fix: believes it already holds it


def test_G2_the_ignored_timeout_is_a_regression(tmp_path):
    """Pre-fix, every call site got the timeout it asked for."""
    locks = tmp_path / "locks"; locks.mkdir()
    assert OldStoreLock(locks, timeout=30.0).timeout == 30.0
    assert OldStoreLock(locks, timeout=0.05).timeout == 0.05     # pre-fix: honoured
    StoreLock(locks, timeout=30.0)
    assert StoreLock(locks, timeout=0.05).timeout == 30.0        # post-fix: silently ignored


def test_G3_a_queue_inside_the_store_is_pushed_to_the_remote(tmp_path):
    """STORE_GITIGNORE hard-codes `.memento/queue/`, but nothing in the engine ever puts the queue
    there and nothing checks where the consumer put it. A queue_root anywhere else under the store
    root is committed and pushed by the drain's `git add -A`. ADR D8: the verbatim pile."""
    from memento.backup import enable_backup, commit_consolidation

    store = MemoryStore(tmp_path / "memory"); store.initialize()
    queue = Queue(store.root / "sessions-data")          # a plausible layout: jubs' own name
    queue.append_turn("s1", 1, {"said": "verbatim transcript material"})

    enable_backup(store, acknowledged=True)
    commit_consolidation(store, "s1")
    tracked = subprocess.run(["git", "ls-files"], cwd=store.root,
                             capture_output=True, text=True).stdout
    assert "sessions-data/s1/journal.jsonl" in tracked
