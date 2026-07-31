"""The deterministic write gates — the anti-sycophancy mechanism (ADR D3.1).

The load-bearing claim under test: an adapter that declares nothing at all still cannot erode a
store, and nothing an adapter can do removes the floor.
"""

from __future__ import annotations

import copy

import pytest

from conftest import base_facts, make_adapter, render_documents
from memento.writepath import UNCHECKED
from memento import Adapter, GateFailure, Proposal, RuleSet, apply_consolidation, current_state
from memento.gates import AntiErosionFloor, NoEntryRewrite, OrderedScaleFloor


def _propose(facts, **kwargs):
    return Proposal(facts=facts, **kwargs)


# ------------------------------------------------------- the floor is the floor


def test_an_adapter_declaring_nothing_still_gets_the_floor():
    bare = Adapter(name="bare")
    kinds = {type(rule) for rule in bare.rule_set().all()}
    assert {AntiErosionFloor, OrderedScaleFloor, NoEntryRewrite} <= kinds


def test_an_empty_adapter_rule_set_fails_closed(store):
    """No schema, no scales, no rules — and dropping a language is still rejected."""
    bare = Adapter(name="bare", render_documents=render_documents)
    apply_consolidation(store, bare, _propose(base_facts()), session="s1", batch="b1", expected_fingerprint=UNCHECKED)

    eroded = base_facts()
    del eroded["languages"]["de"]

    with pytest.raises(GateFailure, match="anti-erosion"):
        apply_consolidation(store, bare, _propose(eroded), session="s2", batch="b2", expected_fingerprint=UNCHECKED)


def test_adapter_rules_are_added_after_the_floor_never_instead_of_it():
    class AlwaysPasses:
        name = "permissive"

        def check(self, current, proposal):
            return []

    rules = RuleSet([AlwaysPasses()])
    assert [type(r) for r in rules.all()][:3] == [AntiErosionFloor, OrderedScaleFloor, NoEntryRewrite]
    assert len(rules.all()) == 4


# --------------------------------------------------------------- anti-erosion


def test_dropping_a_language_without_a_tombstone_is_rejected(seeded, adapter):
    eroded = base_facts()
    del eroded["languages"]["de"]

    with pytest.raises(GateFailure) as exc:
        apply_consolidation(seeded, adapter, _propose(eroded), session="s2", batch="b2", expected_fingerprint=UNCHECKED)
    assert "languages/de" in exc.value.render()


def test_dropping_an_interest_without_a_tombstone_is_rejected(seeded, adapter):
    eroded = base_facts()
    eroded["interests"] = [i for i in eroded["interests"] if i["topic"] != "lighthouses"]

    with pytest.raises(GateFailure, match="interests/lighthouses"):
        apply_consolidation(seeded, adapter, _propose(eroded), session="s2", batch="b2", expected_fingerprint=UNCHECKED)


def test_a_tombstone_in_the_proposal_permits_the_shrink(seeded, adapter):
    shrunk = base_facts()
    del shrunk["languages"]["de"]

    result = apply_consolidation(
        seeded,
        adapter,
        _propose(shrunk, tombstones={"languages/de"}),
        session="s2",
        batch="b2",
        expected_fingerprint=UNCHECKED,
    )
    assert result.ok
    assert "de" not in current_state(seeded, adapter).facts["languages"]


def test_a_tombstone_already_in_the_store_permits_a_later_shrink(seeded, adapter):
    from memento.forgetting import tombstone

    tombstone(seeded, "languages/de", session="op", batch="forget")
    shrunk = base_facts()
    del shrunk["languages"]["de"]

    assert apply_consolidation(seeded, adapter, _propose(shrunk), session="s2", batch="b2", expected_fingerprint=UNCHECKED).ok


def test_dropping_a_field_from_an_entry_is_erosion_too(seeded, adapter):
    eroded = base_facts()
    del eroded["languages"]["fr"]["goals"]

    with pytest.raises(GateFailure, match="languages.fr/goals"):
        apply_consolidation(seeded, adapter, _propose(eroded), session="s2", batch="b2", expected_fingerprint=UNCHECKED)


def test_reordering_a_list_is_not_a_change(seeded, adapter):
    """Members are addressed by identity, so a reordered list is a no-op, not a replacement."""
    reordered = base_facts()
    reordered["interests"].reverse()

    assert apply_consolidation(seeded, adapter, _propose(reordered), session="s2", batch="b2", expected_fingerprint=UNCHECKED).ok


def test_growth_is_always_allowed(seeded, adapter):
    grown = base_facts()
    grown["languages"]["pt"] = {"level": "A1", "confidence": "low", "goals": "read song lyrics"}
    grown["interests"].append({"topic": "meteor showers", "engagement": "low", "notes": "new"})

    assert apply_consolidation(seeded, adapter, _propose(grown), session="s2", batch="b2", expected_fingerprint=UNCHECKED).ok


# -------------------------------------------------------------- ordered scales


def test_an_ordered_scale_may_move_one_step(seeded, adapter):
    moved = base_facts()
    moved["languages"]["fr"]["level"] = "B2"  # B1 -> B2
    assert apply_consolidation(seeded, adapter, _propose(moved), session="s2", batch="b2", expected_fingerprint=UNCHECKED).ok


def test_an_ordered_scale_may_not_jump(seeded, adapter):
    jumped = base_facts()
    jumped["languages"]["fr"]["level"] = "C2"  # B1 -> C2

    with pytest.raises(GateFailure, match="3 steps"):
        apply_consolidation(seeded, adapter, _propose(jumped), session="s2", batch="b2", expected_fingerprint=UNCHECKED)


def test_a_scale_may_not_move_more_than_one_step_downward_either(seeded, adapter):
    """Erosion by degrade is the same failure as erosion by delete, and gets the same answer."""
    dropped = base_facts()
    dropped["languages"]["fr"]["level"] = "A2"
    dropped["languages"]["de"]["level"] = "A1"
    dropped["interests"][0]["engagement"] = "low"  # medium -> low is one step: allowed
    assert apply_consolidation(seeded, adapter, _propose(dropped), session="s2", batch="b2", expected_fingerprint=UNCHECKED).ok


def test_a_value_off_the_declared_scale_is_rejected(seeded, adapter):
    bogus = base_facts()
    bogus["languages"]["fr"]["level"] = "fluent"

    with pytest.raises(GateFailure, match="not on the declared scale"):
        apply_consolidation(seeded, adapter, _propose(bogus), session="s2", batch="b2", expected_fingerprint=UNCHECKED)


# --------------------------------------------------------- schema and identity


def test_a_schema_violation_is_rejected(seeded, adapter):
    bad = base_facts()
    bad["interests"][0]["engagement"] = "enthusiastic"

    with pytest.raises(GateFailure, match="schema|scale"):
        apply_consolidation(seeded, adapter, _propose(bad), session="s2", batch="b2", expected_fingerprint=UNCHECKED)


def test_a_renamed_entry_is_caught_by_the_derived_identity_check(seeded, adapter):
    """Renaming an entry forks it and orphans the original — growth on paper, erosion in fact."""
    proposal = Proposal(
        facts=base_facts(),
        entries={"errors/fr": [{"id": "renamed-by-the-model", "pattern": "je suis 20 ans"}]},
    )
    with pytest.raises(GateFailure, match="derived-identity"):
        apply_consolidation(seeded, adapter, proposal, session="s2", batch="b2", expected_fingerprint=UNCHECKED)


def test_a_correctly_derived_entry_id_passes(seeded, adapter):
    proposal = Proposal(
        facts=base_facts(),
        entries={"errors/fr": [{"id": "fr-je-vais-aller", "pattern": "je vais aller"}]},
    )
    assert apply_consolidation(seeded, adapter, proposal, session="s2", batch="b2", expected_fingerprint=UNCHECKED).ok


def test_an_entry_missing_a_required_field_is_rejected(seeded, adapter):
    proposal = Proposal(facts=base_facts(), entries={"vocab/fr": [{"item": "no id here"}]})
    with pytest.raises(GateFailure, match="entry-schema"):
        apply_consolidation(seeded, adapter, proposal, session="s2", batch="b2", expected_fingerprint=UNCHECKED)


def test_deletion_is_not_an_event(seeded, adapter):
    proposal = Proposal(
        facts=base_facts(),
        entries={"errors/fr": [{"id": "fr-je-suis-20-ans", "event": "delete"}]},
    )
    with pytest.raises(GateFailure, match="append-only"):
        apply_consolidation(seeded, adapter, proposal, session="s2", batch="b2", expected_fingerprint=UNCHECKED)


# ------------------------------------------------------------ all-or-nothing


def test_a_rejected_consolidation_writes_absolutely_nothing(seeded, adapter, queue):
    before_docs = {name: seeded.read_document(name) for name in seeded.documents()}
    before_events = {s: len(seeded.log(s).read()) for s in seeded.streams()}

    bad = base_facts()
    bad["languages"]["fr"]["level"] = "C2"  # a jump, rejected
    proposal = Proposal(
        facts=bad,
        entries={"errors/fr": [{"id": "fr-would-have-been-fine", "pattern": "would have been fine"}]},
    )

    with pytest.raises(GateFailure):
        apply_consolidation(
            seeded, adapter, proposal, session="s2", batch="b2", queue=queue,
            expected_fingerprint=UNCHECKED,
        )

    assert {name: seeded.read_document(name) for name in seeded.documents()} == before_docs
    assert {s: len(seeded.log(s).read()) for s in seeded.streams()} == before_events


def test_a_rejected_consolidation_defers_the_session_and_flags(seeded, adapter, queue):
    from memento.flags import GATE_REJECTED, FlagSink

    queue.close_and_enqueue("s2")
    sink = FlagSink()
    bad = base_facts()
    del bad["languages"]["de"]

    with pytest.raises(GateFailure):
        apply_consolidation(
            seeded, adapter, _propose(bad), session="s2", batch="b2", queue=queue, sink=sink,
            expected_fingerprint=UNCHECKED,
        )

    assert [f.kind for f in sink.flags] == [GATE_REJECTED]
    assert queue.pending_sessions()[0].deferrals == 1
    assert not queue.is_consolidated("s2")


def test_every_violation_is_reported_not_just_the_first(seeded, adapter):
    bad = base_facts()
    del bad["languages"]["de"]
    bad["languages"]["fr"]["level"] = "C2"

    with pytest.raises(GateFailure) as exc:
        apply_consolidation(seeded, adapter, _propose(bad), session="s2", batch="b2", expected_fingerprint=UNCHECKED)

    rendered = exc.value.render()
    assert "languages/de" in rendered and "steps" in rendered
    assert len(exc.value.violations) >= 2


def test_the_baseline_is_read_back_from_the_store_not_carried_in_memory(seeded, adapter, tmp_path):
    """A fresh handle on the same store enforces the same history — state lives on disk."""
    from memento import MemoryStore

    reopened = MemoryStore(seeded.root)
    eroded = base_facts()
    del eroded["languages"]["de"]

    with pytest.raises(GateFailure):
        apply_consolidation(reopened, adapter, _propose(eroded), session="s2", batch="b2", expected_fingerprint=UNCHECKED)


def test_adapter_rules_can_tighten(seeded):
    class NoNewLanguages:
        name = "no-new-languages"

        def check(self, current, proposal):
            from memento.gates import Violation

            new = set(proposal.facts.get("languages", {})) - set(current.facts.get("languages", {}))
            return [Violation(self.name, f"languages/{k}", "adapter forbids new languages") for k in sorted(new)]

    strict = make_adapter(rules=(NoNewLanguages(),))
    grown = copy.deepcopy(base_facts())
    grown["languages"]["pt"] = {"level": "A1", "confidence": "low", "goals": "x"}

    with pytest.raises(GateFailure, match="no-new-languages"):
        apply_consolidation(seeded, strict, _propose(grown), session="s2", batch="b2", expected_fingerprint=UNCHECKED)
