"""Forgetting and the operator verbs (ADR D5): tombstone, never delete."""

from __future__ import annotations

import pytest

from conftest import base_facts
from memento import (
    GateFailure,
    MementoError,
    Proposal,
    apply_consolidation,
    note_contradiction,
    recall,
    retire_entry,
)
from memento.forgetting import drop_member, forget_fact
from memento.writepath import UNCHECKED, TOMBSTONE_STREAM, read_facts, read_tombstones


def test_forget_removes_the_fact_and_leaves_the_tombstone_behind(seeded, adapter):
    forget_fact(seeded, adapter, "languages/de", session="op1", batch="forget1")

    assert "it" not in read_facts(seeded)["languages"]
    assert "languages/de" in read_tombstones(seeded)
    assert "it" not in seeded.read_document("profile.md")


def test_a_forgotten_fact_stays_forgettable_in_later_consolidations(seeded, adapter):
    """"Honored by all future consolidations" — the tombstone outlives the session that wrote it."""
    forget_fact(seeded, adapter, "languages/de", session="op1", batch="forget1")

    later = base_facts()
    del later["languages"]["de"]
    assert apply_consolidation(seeded, adapter, Proposal(facts=later), session="s2", batch="b2", expected_fingerprint=UNCHECKED).ok


def test_forgetting_something_that_is_not_there_is_an_error_not_a_silent_no_op(seeded, adapter):
    with pytest.raises(MementoError, match="no such member"):
        forget_fact(seeded, adapter, "languages/xx", session="op1", batch="forget1")


def test_the_forget_still_goes_through_the_gates(seeded, adapter):
    """The operator gets an exception to the erosion rule *because* of the tombstone, not by fiat."""
    from memento.gates import Violation

    class RefuseEverything:
        name = "refuse-everything"

        def check(self, current, proposal):
            return [Violation(self.name, "*", "adapter says no")]

    from conftest import make_adapter

    stubborn = make_adapter(rules=(RefuseEverything(),))
    with pytest.raises(GateFailure, match="refuse-everything"):
        forget_fact(seeded, stubborn, "languages/de", session="op1", batch="forget1")


def test_retiring_an_entry_hides_it_from_recall_but_not_from_history(seeded):
    assert recall(seeded, "20 ans")

    retire_entry(seeded, "errors/fr", "fr-je-suis-20-ans", session="op1", batch="forget1")

    assert recall(seeded, "20 ans") == []
    folded = seeded.folded("errors/fr")["fr-je-suis-20-ans"]
    assert folded.is_tombstoned and folded.payload["pattern"] == "je suis 20 ans"
    assert "errors/fr/fr-je-suis-20-ans" in read_tombstones(seeded)


def test_nothing_is_ever_removed_from_the_log(seeded):
    before = len(seeded.log("errors/fr").read())
    retire_entry(seeded, "errors/fr", "fr-je-suis-20-ans", session="op1", batch="forget1")
    assert len(seeded.log("errors/fr").read()) == before + 1  # a tombstone is an addition


def test_a_contradiction_is_recorded_for_the_next_consolidation(seeded):
    note_contradiction(
        seeded, "errors/fr", "fr-je-suis-20-ans", session="s2", batch="b2", note="learner disagreed"
    )
    entry = seeded.folded("errors/fr")["fr-je-suis-20-ans"]
    assert entry.contested and entry.is_active  # flagged for correction, not corrected here


def test_tombstones_live_in_their_own_stream(seeded, adapter):
    forget_fact(seeded, adapter, "languages/de", session="op1", batch="forget1")
    assert seeded.log(TOMBSTONE_STREAM).read()


@pytest.mark.parametrize(
    "marker,check",
    [
        ("languages/fr", lambda f: "fr" not in f["languages"]),
        ("interests/kite building", lambda f: [i["topic"] for i in f["interests"]] == ["lighthouses"]),
        ("languages.fr/goals", lambda f: "goals" not in f["languages"]["fr"]),
    ],
)
def test_drop_member_addresses_dicts_lists_and_nested_fields(marker, check):
    assert check(drop_member(base_facts(), marker))


def test_drop_member_refuses_a_path_that_names_nothing():
    with pytest.raises(MementoError):
        drop_member(base_facts(), "languages/xx")
