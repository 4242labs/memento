"""Lock and claim protocol (ADR D3.3): two front-ends, no lost write, no double-paid call."""

from __future__ import annotations

import os
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


# ============================================ the CAS claim, in-process (B-02 T8 R3)

# The cross-process property — a claim that outlives the process which took it — needs real
# subprocesses and lives in the acceptance tier. Everything else about the claim is ordinary state
# on disk and belongs here, where a mutant dies in milliseconds. Excluding the acceptance tier from
# the mutation runner without this file would have left `CasClaim` measured by nothing at all.


def _claim(tmp_path, session="260802-160000", **kwargs):
    from memento.locking import CasClaim

    return CasClaim(tmp_path / "locks", session, **kwargs)


def test_a_claim_records_who_holds_it_and_for_how_long(tmp_path, clock):
    claim = _claim(tmp_path, clock=clock, ttl=120.0)
    record = claim.acquire()

    assert record.session == "260802-160000"
    assert record.pid == os.getpid()
    assert record.ttl == 120.0
    assert record.acquired_at == clock.now()
    assert record.token

    read_back = claim.read()
    assert read_back == record


def test_reading_a_claim_that_was_never_taken_is_not_an_error(tmp_path):
    assert _claim(tmp_path).read() is None


def test_a_torn_claim_file_reads_as_unheld(tmp_path):
    """A crash mid-write must not wedge the session behind an unparseable file."""
    claim = _claim(tmp_path)
    claim.acquire()
    claim.path.write_text("{ not json", encoding="utf-8")

    assert claim.read() is None
    assert claim.acquire().token, "an unreadable claim must be reclaimable"


def test_a_claim_file_without_a_token_reads_as_unheld(tmp_path):
    claim = _claim(tmp_path)
    claim.acquire()
    claim.path.write_text('{"session": "260802-160000", "pid": 1}', encoding="utf-8")

    assert claim.read() is None


def test_a_live_claim_excludes_a_second_claimant(tmp_path, clock):
    from memento.errors import ClaimHeld

    first = _claim(tmp_path, clock=clock, ttl=600.0)
    first.acquire()

    with pytest.raises(ClaimHeld, match="claimed by pid"):
        _claim(tmp_path, clock=clock).acquire()


def test_a_claim_past_its_ttl_is_anyone_s(tmp_path, clock):
    """Every claim expires, or an agent that walked away wedges the session for good."""
    held = _claim(tmp_path, clock=clock, ttl=60.0)
    first = held.acquire()

    clock.advance(59)
    from memento.errors import ClaimHeld

    with pytest.raises(ClaimHeld):
        _claim(tmp_path, clock=clock).acquire()

    clock.advance(2)
    second = _claim(tmp_path, clock=clock).acquire()
    assert second.token != first.token


def test_staleness_is_measured_against_the_recorded_ttl_not_a_global_one(tmp_path, clock):
    from memento.locking import CasClaimRecord

    record = CasClaimRecord(session="s", pid=1, token="t", acquired_at=clock.now(), ttl=10.0)
    assert not record.is_stale(clock.now() + 9)
    assert record.is_stale(clock.now() + 11)


def test_releasing_needs_the_token_back(tmp_path, clock):
    from memento.errors import ClaimHeld

    claim = _claim(tmp_path, clock=clock)
    record = claim.acquire()

    with pytest.raises(ClaimHeld, match="different token"):
        claim.release("not-the-token")
    assert claim.read() is not None, "a refused release must leave the claim standing"

    assert claim.release(record.token) is True
    assert claim.read() is None


def test_releasing_something_nobody_holds_is_false_not_an_error(tmp_path):
    assert _claim(tmp_path).release("whatever") is False


def test_reacquiring_after_a_release_issues_a_new_token(tmp_path, clock):
    claim = _claim(tmp_path, clock=clock)
    first = claim.acquire()
    claim.release(first.token)
    second = claim.acquire()

    assert second.token != first.token


def test_cas_claims_lists_every_claim_oldest_first(tmp_path, clock):
    from memento.locking import cas_claims

    _claim(tmp_path, "260802-000002", clock=clock).acquire()
    clock.advance(10)
    _claim(tmp_path, "260802-000001", clock=clock).acquire()

    listed = cas_claims(tmp_path / "locks")
    assert [r.session for r in listed] == ["260802-000002", "260802-000001"]


def test_cas_claims_on_a_directory_that_does_not_exist_is_empty(tmp_path):
    from memento.locking import cas_claims

    assert cas_claims(tmp_path / "nowhere") == []


def test_cas_claims_skips_an_artifact_whose_session_id_does_not_validate(tmp_path, clock):
    """Reporting is a courtesy; letting it raise would kill every drain before it looked at one."""
    from memento.locking import cas_claims

    _claim(tmp_path, clock=clock).acquire()
    (tmp_path / "locks" / "claim-../escape.json").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "locks" / "claim-.json").write_text('{"token": "x", "pid": 1}', encoding="utf-8")

    assert [r.session for r in cas_claims(tmp_path / "locks")] == ["260802-160000"]


def test_a_claim_id_that_is_not_a_path_segment_is_refused(tmp_path):
    from memento.errors import MementoError

    with pytest.raises(MementoError, match="invalid session id"):
        _claim(tmp_path, "../../escape")


def test_the_claim_file_is_written_atomically(tmp_path, clock):
    """Via a temp file and a rename, so a reader never sees a half-written claim."""
    claim = _claim(tmp_path, clock=clock)
    claim.acquire()

    leftovers = list((tmp_path / "locks").glob("*.tmp"))
    assert not leftovers, f"a temp file survived the write: {leftovers}"


def test_a_partial_claim_file_reads_with_defaults_rather_than_raising(tmp_path):
    """The only field that must be there is the token — it is what a release is checked against.

    Everything else has a default, because a claim written by an older build must still be readable
    by this one: an unreadable claim is an unreclaimable session.
    """
    claim = _claim(tmp_path, "260802-160000")
    claim.locks_dir.mkdir(parents=True, exist_ok=True)
    claim.path.write_text('{"token": "abc"}', encoding="utf-8")

    record = claim.read()
    assert record is not None
    assert record.token == "abc"
    assert record.session == "260802-160000"  # falls back to the claim's own session
    assert record.pid == 0
    assert record.acquired_at == 0.0
    assert record.ttl == claim.ttl  # the reader's TTL, not a hard-coded one


def test_every_recorded_field_survives_a_round_trip(tmp_path):
    claim = _claim(tmp_path, "260802-160000", ttl=42.0)
    claim.locks_dir.mkdir(parents=True, exist_ok=True)
    claim.path.write_text(
        '{"session": "260802-160001", "pid": 4242, "token": "tok", '
        '"acquired_at": 1785000123.5, "ttl": 7.5}',
        encoding="utf-8",
    )

    record = claim.read()
    assert (record.session, record.pid, record.token) == ("260802-160001", 4242, "tok")
    assert record.acquired_at == 1785000123.5
    assert record.ttl == 7.5  # the file's TTL wins over the reader's


def test_an_empty_claim_file_reads_as_unheld(tmp_path):
    claim = _claim(tmp_path)
    claim.locks_dir.mkdir(parents=True, exist_ok=True)
    claim.path.write_text("", encoding="utf-8")

    assert claim.read() is None


def test_a_claim_token_is_not_guessable_from_the_session(tmp_path, clock):
    """Two claims on the same session, taken in sequence, never reuse a token."""
    claim = _claim(tmp_path, clock=clock, ttl=1.0)
    first = claim.acquire()
    clock.advance(5)
    second = claim.acquire()

    assert first.token != second.token
    assert len(second.token) == 16  # 8 random bytes, hex


def test_the_claim_directory_is_created_on_demand(tmp_path):
    claim = _claim(tmp_path)
    assert not claim.locks_dir.exists()

    claim.acquire()
    assert claim.path.exists()
