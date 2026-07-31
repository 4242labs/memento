"""The queue — the store's second, unversioned area. Marker-LAST, backlog bounds, retention."""

from __future__ import annotations

import pytest

from memento import Queue, RetentionPolicy


def test_session_exit_only_closes_and_enqueues(queue, store):
    """Exit does no git work and no store write. That is what keeps it under five seconds."""
    queue.append_turn("s1", 1, {"said": "hello"})
    queue.close_and_enqueue("s1")

    assert [p.session for p in queue.pending_sessions()] == ["s1"]
    assert not queue.is_consolidated("s1")
    assert not (store.root / ".git").exists()
    assert store.documents() == []


def test_the_marker_is_what_makes_a_session_consolidated(queue):
    queue.close_and_enqueue("s1")
    assert queue.pending_sessions()
    queue.mark_consolidated("s1")
    assert queue.pending_sessions() == []
    assert queue.marker_path("s1").exists()


def test_pending_sessions_come_back_oldest_first(queue, clock):
    queue.close_and_enqueue("s2")
    clock.advance(60)
    queue.close_and_enqueue("s1")
    assert [p.session for p in queue.pending_sessions()] == ["s2", "s1"]


def test_deferrals_are_counted_not_dropped(queue):
    queue.close_and_enqueue("s1")
    queue.mark_deferred("s1", "model unavailable")
    queue.mark_deferred("s1", "model unavailable again")
    assert queue.pending_sessions()[0].deferrals == 2


def test_the_backlog_bound_flags_by_count(queue):
    for i in range(7):
        queue.close_and_enqueue(f"s{i}")
    status = queue.backlog(max_pending=5)
    assert status.breached and "exceeds the bound" in (status.message() or "")


def test_the_backlog_bound_flags_by_age(queue, clock):
    queue.close_and_enqueue("s1")
    clock.advance(86400 * 10)
    status = queue.backlog(max_pending=50, max_age_days=7)
    assert status.breached and "stale" in (status.message() or "")


def test_a_healthy_backlog_says_nothing(queue):
    queue.close_and_enqueue("s1")
    assert queue.backlog().message() is None


def test_a_torn_journal_line_does_not_lose_the_rest(queue):
    queue.append_turn("s1", 1, {"said": "one"})
    queue.append_turn("s1", 2, {"said": "two"})
    path = queue.session_dir("s1") / "journal.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"turn": 3, "sai')

    assert [r["turn"] for r in queue.read_journal("s1")] == [1, 2]


def test_keep_everything_is_the_default_and_prune_is_a_no_op(queue):
    queue.append_turn("s1", 1, {"said": "hello"})
    queue.close_and_enqueue("s1")
    queue.mark_consolidated("s1")

    assert queue.prune("s1") is False
    assert queue.read_journal("s1")  # "not in the memory store" never means "transient"


def test_pruning_requires_both_a_policy_and_a_marker(tmp_path, clock):
    queue = Queue(
        tmp_path / "sessions-data",
        clock=clock,
        retention=RetentionPolicy(keep_everything=False, prune_after_consolidation=True),
    )
    queue.append_turn("s1", 1, {"said": "hello"})
    queue.close_and_enqueue("s1")

    assert queue.prune("s1") is False  # not consolidated yet: nothing is dropped
    assert queue.read_journal("s1")

    queue.mark_consolidated("s1")
    assert queue.read_journal("s1") == []  # marking consolidated applies the policy


def test_a_contradictory_retention_policy_is_refused():
    with pytest.raises(ValueError):
        RetentionPolicy(keep_everything=True, prune_after_consolidation=True)
