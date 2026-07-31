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
