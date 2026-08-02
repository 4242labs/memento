"""The read path: a budget that is actually enforced, and recall that stays on topic."""

from __future__ import annotations

import pytest

from conftest import make_adapter
from memento import BudgetError, PrefixSection, assemble_prefix, recall
from memento.flags import PREFIX_TRUNCATED, FlagSink
from memento.tokenizer import HeuristicCounter

TOPICS = ["kites", "lighthouses", "meteors", "ferments", "cartography", "birdsong"]


def _sections(*specs):
    return tuple(
        PrefixSection(name=name, priority=priority, render=lambda s, t=text: t, required=required)
        for name, priority, text, required in specs
    )


def test_the_prefix_fits_the_budget(store):
    adapter = make_adapter(
        prefix_budget_tokens=20,
        prefix_sections=_sections(
            ("a", 0, "alpha beta gamma", False),
            ("b", 1, " ".join(["word"] * 200), False),
        ),
    )
    result = assemble_prefix(store, adapter)
    assert result.tokens <= 20
    assert HeuristicCounter().count(result.text) <= 20


def test_truncation_follows_the_declared_priority_order(store):
    adapter = make_adapter(
        prefix_budget_tokens=12,
        prefix_sections=_sections(
            ("high", 0, "one two three", False),
            ("mid", 1, "four five six", False),
            ("low", 2, "\n".join(f"line {i}" for i in range(50)), False),
        ),
    )
    result = assemble_prefix(store, adapter)

    assert "one two three" in result.text
    assert "four five six" in result.text
    assert result.truncated + result.dropped == ["low"]


def test_truncation_cuts_whole_lines_never_mid_word(store):
    adapter = make_adapter(
        prefix_budget_tokens=14,
        prefix_sections=_sections(("only", 0, "\n".join(f"line {i}" for i in range(20)), False)),
    )
    result = assemble_prefix(store, adapter)
    assert result.text
    for line in result.text.splitlines():
        assert line.startswith("line ")


def test_overflow_is_never_silent(store):
    adapter = make_adapter(
        prefix_budget_tokens=6,
        prefix_sections=_sections(("big", 0, "\n".join(f"line {i}" for i in range(50)), False)),
    )
    sink = FlagSink()
    result = assemble_prefix(store, adapter, sink=sink)

    assert [f.kind for f in sink.flags] == [PREFIX_TRUNCATED]
    assert result.flags and result.truncated


def test_a_required_section_that_cannot_fit_is_an_error_not_a_shrug(store):
    adapter = make_adapter(
        prefix_budget_tokens=2,
        prefix_sections=_sections(("must-have", 0, "a b c d e f g h i j k l", True)),
    )
    with pytest.raises(BudgetError, match="required prefix section"):
        assemble_prefix(store, adapter)


def test_a_non_local_counter_is_refused_on_the_hot_path(store):
    class NetworkCounter:
        name = "provider.count_tokens"
        is_local = False

        def count(self, text: str) -> int:  # pragma: no cover - never reached
            raise AssertionError("this would have been a network call")

    adapter = make_adapter(token_counter=NetworkCounter())
    with pytest.raises(BudgetError, match="not local"):
        assemble_prefix(store, adapter)


def test_assembly_is_deterministic(seeded, adapter):
    first = assemble_prefix(seeded, adapter)
    second = assemble_prefix(seeded, adapter)
    assert first.text == second.text and first.tokens == second.tokens


def test_the_prefix_carries_the_documents(seeded, adapter):
    result = assemble_prefix(seeded, adapter, budget=2000)
    assert "## fr" in result.text
    assert "kite building" in result.text


# ------------------------------------------------------------------- recall


def _grow(store, per_topic: int):
    for topic in TOPICS:
        store.append(
            f"vocab/{topic}",
            [
                {"id": f"{topic}-{i}", "item": f"{topic} term {i}", "context": f"discussing {topic}"}
                for i in range(per_topic)
            ],
            session=f"s-{topic}-{per_topic}",
            batch=f"b-{per_topic}",
        )


@pytest.mark.parametrize("per_topic", [5, 40, 200])
def test_recall_never_contaminates_across_topics_as_the_store_grows(store, per_topic):
    """The probe test. Growth must not start dragging neighbouring topics into an answer."""
    _grow(store, per_topic)

    hits = recall(store, "kites", limit=10)

    assert hits, "the probe found nothing at all"
    for hit in hits:
        assert "kites" in f"{hit.location} {hit.entry_id} {hit.text}".lower()


def test_recall_returns_nothing_rather_than_the_nearest_thing(store):
    _grow(store, 10)
    assert recall(store, "quantum chromodynamics") == []


def test_recall_is_capped_so_the_archive_is_never_bulk_loaded(store):
    _grow(store, 200)
    assert len(recall(store, "kites", limit=5)) == 5


def test_recall_skips_retired_entries(store):
    store.append("vocab/fr", [{"id": "v1", "item": "kites"}], session="s1", batch="b1")
    assert recall(store, "kites")

    store.append("vocab/fr", [{"id": "v1", "event": "retired"}], session="s2", batch="b2")
    assert recall(store, "kites") == []
    assert recall(store, "kites", include_retired=True)


def test_recall_reads_documents_too(seeded):
    hits = recall(seeded, "lighthouses")
    assert any(h.source == "document" for h in hits)


def test_recall_ignores_the_engine_area(seeded):
    """`.memento/` holds facts and tombstones, not memories. It is not a search surface."""
    assert all(not h.location.startswith(".memento/") for h in recall(seeded, "lighthouses"))


def test_recall_ordering_is_deterministic(store):
    _grow(store, 20)
    assert recall(store, "kites term 3", limit=8) == recall(store, "kites term 3", limit=8)


# ------------------------------------------------- recall: budget and filters (B-02 T6)


def test_a_recall_budget_bounds_the_answer_by_cost_not_just_by_count(store, adapter):
    """`--limit` bounds how many hits come back; it does not bound how much text they are.

    An agent pastes recall output into its own context, so the number that matters is tokens. Ten
    hits over a long stream is not a bounded amount of prompt.
    """
    _grow(store, 40)
    unbounded = recall(store, "kites term", limit=25)
    assert len(unbounded) == 25

    result = recall(store, "kites term", limit=25, budget=60, counter=adapter.token_counter)
    assert result.tokens <= 60
    assert len(result) < len(unbounded)
    assert result.dropped == len(unbounded) - len(result)


def test_what_the_budget_cut_is_reported_never_dropped_quietly(store, adapter):
    _grow(store, 40)
    result = recall(store, "kites term", limit=25, budget=60, counter=adapter.token_counter)

    assert result.truncated
    assert result.flags and "budget" in result.flags[0].message
    assert result.counter == "memento.heuristic.v1"


def test_the_budget_cuts_the_least_relevant_end_deterministically(store, adapter):
    """Ordered by score before the cut, so the same query keeps the same answer."""
    _grow(store, 40)
    first = recall(store, "kites term 3", limit=25, budget=80, counter=adapter.token_counter)
    second = recall(store, "kites term 3", limit=25, budget=80, counter=adapter.token_counter)

    assert first.hits == second.hits
    scores = [h.score for h in first]
    assert scores == sorted(scores, reverse=True)


def test_a_budget_without_a_counter_is_refused(store):
    with pytest.raises(BudgetError, match="token counter"):
        recall(store, "kites", budget=50)


def test_a_budget_may_not_be_counted_over_the_network(store):
    class Remote:
        name = "remote-counter"
        is_local = False

        def count(self, text):  # pragma: no cover - never reached
            raise AssertionError("the read path made a network call")

    _grow(store, 5)
    with pytest.raises(BudgetError, match="not local"):
        recall(store, "kites", budget=50, counter=Remote())


def test_recall_filters_to_named_streams(store):
    _grow(store, 5)
    hits = recall(store, "term", streams=["vocab/kites"])
    assert hits and all(h.location == "vocab/kites" for h in hits)


def test_recall_filters_to_named_entry_ids(store):
    _grow(store, 5)
    hits = recall(store, "term", keys=["kites-2"])
    assert [h.entry_id for h in hits] == ["kites-2"]


def test_recall_filters_by_date_range(store, clock):
    """ISO-8601 sorts in time order, so the range compare is a string compare and nothing else."""
    store.append("vocab/fr", [{"id": "old", "item": "kites long ago"}], session="s1", batch="b1")
    clock.advance(30 * 86400)
    boundary = clock.now_iso()
    clock.advance(86400)
    store.append("vocab/fr", [{"id": "new", "item": "kites recently"}], session="s2", batch="b2")

    assert [h.entry_id for h in recall(store, "kites", since=boundary)] == ["new"]
    assert [h.entry_id for h in recall(store, "kites", until=boundary)] == ["old"]
    assert len(recall(store, "kites")) == 2


def test_an_entry_with_no_timestamp_is_outside_every_range(store, clock):
    """Outside every range, never inside all of them — a missing stamp is not a wildcard."""
    (store.root / "vocab").mkdir(parents=True, exist_ok=True)
    (store.root / "vocab" / "pt.jsonl").write_text(
        '{"id": "undated", "event": "entry", "item": "kites"}\n', encoding="utf-8"
    )
    assert recall(store, "kites")
    assert recall(store, "kites", since="2000-01-01T00:00:00Z") == []


def test_a_date_ranged_recall_leaves_the_projected_documents_out(seeded):
    """A document is the *current* state and carries no per-line history.

    Including one in a time-ranged answer would date it to whenever the reader happened to look,
    which is a worse answer than no answer.
    """
    assert any(h.source == "document" for h in recall(seeded, "lighthouses"))
    ranged = recall(seeded, "lighthouses", since="2000-01-01T00:00:00Z")
    assert all(h.source == "event" for h in ranged)


def test_an_event_carries_the_timestamp_the_filters_read(store):
    store.append("vocab/fr", [{"id": "v1", "item": "kites"}], session="s1", batch="b1")
    (hit,) = recall(store, "kites")
    assert hit.ts and hit.ts.endswith("Z")
