"""Crash windows (ADR D6).

Every one of these is a real interruption point, not a simulation of one: the process dies between
two writes and the next run has to reach the same end state without duplicating anything.

The invariant under all of them: **crash before the marker ⇒ re-run, never lose.**
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from conftest import StubDistiller, base_facts
from memento import Proposal, SessionClaim, run_drain
from memento.locking import stale_claims


def test_a_failed_drain_leaves_the_session_pending_and_reclaimable(store, adapter, queue, clock):
    queue.close_and_enqueue("s1")

    dead = StubDistiller(error=RuntimeError("model went away mid-call"))
    report = run_drain(store, adapter, dead, queue, clock=clock)

    assert report.consolidated == [] and report.deferred
    assert not queue.is_consolidated("s1")
    assert [p.session for p in queue.pending_sessions()] == ["s1"]

    good = StubDistiller(proposal=Proposal(facts=base_facts()))
    run_drain(store, adapter, good, queue, clock=clock)

    assert queue.is_consolidated("s1")
    assert queue.pending_sessions() == []


def test_a_claim_dies_with_its_holder_and_is_reclaimed(store, tmp_path):
    """A hard kill, in a real subprocess. The OS releases the flock; the next drain picks it up."""
    script = textwrap.dedent(
        f"""
        from memento import SessionClaim
        SessionClaim({str(store.locks_dir)!r}, "s1").acquire()
        # exit without releasing: the process dies holding the claim
        """
    )
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)

    assert (store.locks_dir / "claim-s1.lock").exists()  # the artifact outlives the holder
    reclaimed = SessionClaim(store.locks_dir, "s1").acquire()  # ...but the lock does not
    reclaimed.release()


def test_a_stale_claim_is_visible_before_it_is_reclaimed(store, adapter, queue, clock):
    """A wedged-but-alive holder must be reported, not silently waited on."""
    claim = SessionClaim(store.locks_dir, "s1", clock=clock)
    claim.acquire()
    claim.release()
    clock.advance(7200)

    stale = stale_claims(store.locks_dir, now=clock.now(), ttl=3600)
    assert [c.session for c in stale] == ["s1"]

    queue.close_and_enqueue("s1")
    report = run_drain(
        store, adapter, StubDistiller(proposal=Proposal(facts=base_facts())), queue, clock=clock
    )
    assert any(f.kind == "stale-claim" for f in report.flags)
    assert report.consolidated == ["s1"]  # flagged, and still drained


def test_a_crash_between_the_store_write_and_the_marker_replays_cleanly(
    store, adapter, queue, clock, monkeypatch
):
    """The marker-LAST window. The re-run must land the same state and duplicate nothing."""
    queue.close_and_enqueue("s1")
    proposal = Proposal(
        facts=base_facts(),
        entries={"vocab/fr": [{"id": "v1", "item": "word"}]},
        session_log="log text",
    )

    def die(session):
        raise RuntimeError("power cut before the marker")

    monkeypatch.setattr(type(queue), "mark_consolidated", lambda self, session: die(session))
    with pytest.raises(RuntimeError):
        run_drain(store, adapter, StubDistiller(proposal=proposal), queue, clock=clock)

    assert not queue.is_consolidated("s1")  # so the session is still pending, by design
    events_after_crash = len(store.log("vocab/fr").read())
    docs_after_crash = len(store.document_log().read())

    monkeypatch.undo()
    run_drain(store, adapter, StubDistiller(proposal=proposal), queue, clock=clock)

    assert queue.is_consolidated("s1")
    assert len(store.log("vocab/fr").read()) == events_after_crash  # idempotent batch, no duplicate
    assert len(store.document_log().read()) == docs_after_crash


def test_a_claim_held_by_a_live_process_blocks_a_second_drain(store, adapter, queue, clock):
    queue.close_and_enqueue("s1")
    held = SessionClaim(store.locks_dir, "s1").acquire()
    try:
        report = run_drain(
            store, adapter, StubDistiller(proposal=Proposal(facts=base_facts())), queue, clock=clock
        )
        assert report.skipped == ["s1"] and report.consolidated == []
        assert not queue.is_consolidated("s1")
    finally:
        held.release()

    # Released, so the next drain gets through.
    report = run_drain(
        store, adapter, StubDistiller(proposal=Proposal(facts=base_facts())), queue, clock=clock
    )
    assert report.consolidated == ["s1"]
