"""The drain: spawn gating, the detached child, and bounded visible deferral (ADR D3.2/D3.6)."""

from __future__ import annotations

import os
import time

import pytest

from conftest import StubDistiller, base_facts
from memento import (
    DrainGate,
    DrainRefused,
    Proposal,
    Queue,
    backlog_flag,
    run_drain,
    spawn_drain,
)
from memento.flags import BACKLOG, LLM_UNAVAILABLE, FlagSink
from memento.writepath import read_facts


# ------------------------------------------------------------------ the gate


def test_the_gate_refuses_before_the_prefix_is_materialized():
    gate = DrainGate(prefix_materialized=False, idle_seconds=60)
    assert not gate.allows()
    assert "tear the composite read" in (gate.refusal() or "")


def test_the_gate_refuses_while_the_session_is_active():
    gate = DrainGate(prefix_materialized=True, idle_seconds=1.0, min_idle_seconds=5.0)
    assert not gate.allows()
    assert "idle" in (gate.refusal() or "")


def test_the_gate_allows_once_both_conditions_hold():
    assert DrainGate(prefix_materialized=True, idle_seconds=30).allows()


def test_spawning_outside_the_gate_raises_rather_than_starting_anyway(tmp_path):
    with pytest.raises(DrainRefused):
        spawn_drain(
            store_root=tmp_path / "memory",
            queue_root=tmp_path / "sessions-data",
            adapter_ref="fixture_consumer:ADAPTER",
            distiller_ref="fixture_consumer:DISTILLER",
            gate=DrainGate(prefix_materialized=False, idle_seconds=0),
        )


# ------------------------------------------------------------------ draining


def test_a_drain_consolidates_and_marks_the_marker_last(store, adapter, queue, clock):
    queue.append_turn("s1", 1, {"said": "hello"})
    queue.close_and_enqueue("s1")
    distiller = StubDistiller(
        proposal=Proposal(
            facts=base_facts(),
            entries={"vocab/fr": [{"id": "v1", "item": "word"}]},
            session_log="what happened",
        )
    )

    report = run_drain(store, adapter, distiller, queue, clock=clock)

    assert report.consolidated == ["s1"]
    assert queue.is_consolidated("s1")
    assert read_facts(store)["languages"]["fr"]["level"] == "B1"
    assert store.read_session_log("s1") == "what happened"


def test_the_distiller_sees_the_journal_and_the_current_state(store, adapter, queue, clock):
    queue.append_turn("s1", 1, {"said": "hello"})
    queue.append_turn("s1", 2, {"said": "goodbye"})
    queue.close_and_enqueue("s1")

    seen = {}
    distiller = StubDistiller(
        proposal=Proposal(facts=base_facts()),
        on_call=lambda journal, state: seen.update(turns=len(journal), facts=state.facts),
    )
    run_drain(store, adapter, distiller, queue, clock=clock)

    assert seen["turns"] == 2
    assert seen["facts"] == {}  # an empty store starts empty; nothing is invented


def test_an_unavailable_model_defers_visibly_and_is_never_fatal(store, adapter, queue, clock):
    queue.close_and_enqueue("s1")
    sink = FlagSink()

    report = run_drain(
        store, adapter, StubDistiller(error=ConnectionError("no route")), queue, sink=sink, clock=clock
    )

    assert report.deferred and report.consolidated == []
    assert [f.kind for f in sink.flags] == [LLM_UNAVAILABLE]
    assert not queue.is_consolidated("s1")
    assert queue.pending_sessions()[0].deferrals == 1


def test_a_session_consolidated_mid_call_is_not_written_twice(store, adapter, queue, clock):
    """The post-call backstop: another front-end finished while this model call was in flight."""
    queue.close_and_enqueue("s1")

    def finish_behind_our_back(journal, state):
        queue.mark_consolidated("s1")

    report = run_drain(
        store,
        adapter,
        StubDistiller(proposal=Proposal(facts=base_facts()), on_call=finish_behind_our_back),
        queue,
        clock=clock,
    )

    assert report.skipped == ["s1"] and report.consolidated == []
    assert read_facts(store) == {}  # nothing was written on top of the other front-end's work


def test_max_sessions_bounds_one_drain(store, adapter, queue, clock):
    for i in range(4):
        queue.close_and_enqueue(f"s{i}")
    report = run_drain(
        store,
        adapter,
        StubDistiller(proposal=Proposal(facts=base_facts())),
        queue,
        max_sessions=2,
        clock=clock,
    )
    assert len(report.consolidated) == 2
    assert len(queue.pending_sessions()) == 2


def test_the_backlog_flag_surfaces_at_session_start(queue):
    for i in range(7):
        queue.close_and_enqueue(f"s{i}")
    sink = FlagSink()
    backlog_flag(queue, sink, max_pending=5)
    assert [f.kind for f in sink.flags] == [BACKLOG]


# ------------------------------------------------- the real detached child


def test_the_detached_child_actually_drains(tmp_path, monkeypatch):
    """End-to-end through `spawn_drain`: a real subprocess, no model, no credentials."""
    monkeypatch.setenv("PYTHONPATH", str(os.path.dirname(__file__)))
    store_root = tmp_path / "memory"
    queue_root = tmp_path / "sessions-data"
    queue = Queue(queue_root)
    queue.append_turn("s1", 1, {"said": "hello"})
    queue.close_and_enqueue("s1")

    proc = spawn_drain(
        store_root=store_root,
        queue_root=queue_root,
        adapter_ref="fixture_consumer:ADAPTER",
        distiller_ref="fixture_consumer:DISTILLER",
        gate=DrainGate(prefix_materialized=True, idle_seconds=60),
    )
    assert proc.wait(timeout=60) == 0

    assert queue.is_consolidated("s1")
    assert (store_root / "profile.md").exists()
    assert "subprocess" in (store_root / "interests.md").read_text()


def test_the_child_runs_in_its_own_session_so_it_outlives_the_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(os.path.dirname(__file__)))
    queue = Queue(tmp_path / "sessions-data")
    queue.close_and_enqueue("s1")

    proc = spawn_drain(
        store_root=tmp_path / "memory",
        queue_root=tmp_path / "sessions-data",
        adapter_ref="fixture_consumer:ADAPTER",
        distiller_ref="fixture_consumer:DISTILLER",
        gate=DrainGate(prefix_materialized=True, idle_seconds=60),
    )
    deadline = time.monotonic() + 60
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)

    assert proc.returncode == 0
    assert os.getpgid(os.getpid()) != proc.pid  # detached into its own process group
