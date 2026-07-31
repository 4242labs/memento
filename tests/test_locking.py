"""Lock and claim protocol (ADR D3.3): two front-ends, no lost write, no double-paid call."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading

import pytest

from conftest import StubDistiller, base_facts
from memento import ClaimHeld, LockTimeout, Proposal, Queue, SessionClaim, StoreLock, run_drain


def test_two_handles_on_one_store_compose_instead_of_deadlocking(store):
    """They are ordinary objects; the reentrancy state is what they share.

    Caching the whole instance instead made a second construction silently ignore the timeout it
    was handed, and left a forked child holding an inherited depth of 1 — writing inside its
    parent's critical section without ever taking the flock.
    """
    lock = StoreLock(store.locks_dir, timeout=0.2)
    other = StoreLock(store.locks_dir, timeout=5.0)
    assert lock is not other
    assert lock.timeout == 0.2 and other.timeout == 5.0  # each keeps what it was given

    with lock.hold():
        with other.hold():
            assert lock.held and other.held
    assert not lock.held


def test_the_store_lock_excludes_another_process(store):
    """Across processes there is no shared object — only the flock, which must still hold."""
    script = textwrap.dedent(
        f"""
        from memento import StoreLock
        from memento.errors import LockTimeout
        try:
            with StoreLock({str(store.locks_dir)!r}, timeout=0.3).hold():
                raise SystemExit(0)   # got in: the lock did not exclude us
        except LockTimeout:
            raise SystemExit(7)
        """
    )
    with StoreLock(store.locks_dir).hold():
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True)
    assert proc.returncode == 7


def test_the_store_lock_excludes_another_thread(store):
    """And within the process the exclusion is the RLock's, not the file's."""
    lock = StoreLock(store.locks_dir, timeout=0.3)
    outcome: list[str] = []
    released = threading.Event()

    def contender():
        try:
            with lock.hold(timeout=0.3):
                outcome.append("entered")
        except LockTimeout:
            outcome.append("blocked")

    with lock.hold():
        t = threading.Thread(target=contender)
        t.start()
        t.join(timeout=5)
    released.set()

    assert outcome == ["blocked"]
    assert not lock.held  # and the state did not underflow on the way out


def test_the_store_lock_is_reentrant_within_one_handle(store):
    lock = StoreLock(store.locks_dir, timeout=0.2)
    with lock.hold():
        with lock.hold():
            assert lock.held
    assert not lock.held


def test_a_claim_is_refused_while_a_live_holder_has_it(store):
    held = SessionClaim(store.locks_dir, "s1").acquire()
    try:
        with pytest.raises(ClaimHeld):
            SessionClaim(store.locks_dir, "s1").acquire()
    finally:
        held.release()

    SessionClaim(store.locks_dir, "s1").acquire().release()  # released: freely reclaimable


def test_claims_are_per_session_not_per_store(store):
    a = SessionClaim(store.locks_dir, "s1").acquire()
    b = SessionClaim(store.locks_dir, "s2").acquire()
    a.release()
    b.release()


def test_the_claim_is_never_the_consolidated_marker(store, queue):
    """Two distinct artifacts. Marker-LAST is load-bearing and nothing may stand in for it."""
    queue.close_and_enqueue("s1")
    claim = SessionClaim(store.locks_dir, "s1").acquire()
    try:
        assert not queue.is_consolidated("s1")
        assert not queue.marker_path("s1").exists()
        assert claim.path.parent == store.locks_dir
        assert queue.marker_path("s1").parent != store.locks_dir
    finally:
        claim.release()
    assert not queue.is_consolidated("s1")


def test_two_drains_never_both_pay_for_one_consolidation(store, adapter, tmp_path, clock):
    """The contention case: one session, two front-ends, exactly one model call."""
    queue = Queue(tmp_path / "sessions-data", clock=clock)
    queue.close_and_enqueue("s1")

    in_call = threading.Event()
    may_finish = threading.Event()

    def hold_open(journal, state):
        in_call.set()
        assert may_finish.wait(timeout=5), "the second drain never got out of the way"

    distiller = StubDistiller(proposal=Proposal(facts=base_facts()), on_call=hold_open)
    reports = []

    def drain():
        reports.append(
            run_drain(store, adapter, distiller, queue, lock=StoreLock(store.locks_dir), clock=clock)
        )

    first = threading.Thread(target=drain)
    first.start()
    assert in_call.wait(timeout=5), "the first drain never reached the model call"

    # Second front-end, while the first is mid-call: it must find the session claimed and skip.
    second = run_drain(store, adapter, distiller, queue, lock=StoreLock(store.locks_dir), clock=clock)
    assert second.skipped == ["s1"] and second.consolidated == []

    may_finish.set()
    first.join(timeout=10)

    assert distiller.calls == 1
    assert [s for r in reports for s in r.consolidated] == ["s1"]
    assert queue.is_consolidated("s1")


def test_two_drains_on_different_sessions_both_land(store, adapter, tmp_path, clock):
    queue = Queue(tmp_path / "sessions-data", clock=clock)
    queue.close_and_enqueue("s1")
    queue.close_and_enqueue("s2")

    facts = base_facts()
    distiller = StubDistiller(proposal=Proposal(facts=facts))

    def drain():
        run_drain(store, adapter, distiller, queue, lock=StoreLock(store.locks_dir), clock=clock)

    threads = [threading.Thread(target=drain) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert queue.is_consolidated("s1") and queue.is_consolidated("s2")
    assert queue.pending_sessions() == []


def test_the_store_lock_is_not_held_across_the_model_call(store, adapter, tmp_path, clock):
    """The lock ordering rule, asserted from inside the call itself."""
    queue = Queue(tmp_path / "sessions-data", clock=clock)
    queue.close_and_enqueue("s1")
    acquired_during_call = []

    def probe(journal, state):
        other = StoreLock(store.locks_dir, timeout=0.5)
        with other.hold():
            acquired_during_call.append(True)

    distiller = StubDistiller(proposal=Proposal(facts=base_facts()), on_call=probe)
    run_drain(store, adapter, distiller, queue, clock=clock)

    assert acquired_during_call == [True]
