"""Declared domain rules: an adapter in JSON may tighten a gate, never widen one (B-02 T4/AC-2).

A Python adapter tightens by shipping a `Rule`. A declared one cannot ship code, so before this it
got the floor and nothing else — which meant the consumer class with the *least* ability to police
its own model also had the weakest write discipline available to it. This closes that, with one
constraint: every rule below only ever removes proposals from the accepted set.

Loosening is refused at **load**, not at consolidation. A spec that would widen a gate is a spec
error, and finding out at write time means finding out after the model already produced something.
"""

from __future__ import annotations

import json

import pytest

from memento import MemoryStore, Proposal, adapter_from_spec, apply_consolidation
from memento.errors import GateFailure, MementoError
from memento.gates import MAX_SCALE_STEP, OrderedScaleFloor, StoreState
from memento.writepath import UNCHECKED, facts_fingerprint, read_facts

BASE = {
    "name": "tightened",
    "identity_keys": ["id", "topic", "name"],
    "documents": {"operator.md": {"title": "Operator", "sections": ["operator", "practice"]}},
    "ordered_scales": {"operator.confidence": ["low", "medium", "high"]},
}

FACTS = {
    "operator": {"confidence": "medium", "reply_style": "terse"},
    "practice": [{"topic": "verify", "weight": "high"}, {"topic": "escalate", "weight": "high"}],
}


def _spec(**overrides):
    return adapter_from_spec({**BASE, **overrides})


def _seed(store, adapter, facts=None):
    apply_consolidation(
        store, adapter, Proposal(facts=facts or FACTS), session="s1", batch="b1",
        expected_fingerprint=UNCHECKED,
    )


# ------------------------------------------------------------------ regex patterns


def test_a_declared_pattern_constrains_a_field(store):
    """The JSON-expressible half of `FieldSpec.check` — text constraints without a callable."""
    adapter = _spec(schema={"operator.reply_style": {"type": "str", "pattern": "^[a-z]+$"}})
    _seed(store, adapter)

    loud = json.loads(json.dumps(FACTS))
    loud["operator"]["reply_style"] = "TERSE!"
    with pytest.raises(GateFailure, match="does not match"):
        apply_consolidation(
            store, adapter, Proposal(facts=loud), session="s2", batch="b2",
            expected_fingerprint=facts_fingerprint(read_facts(store, adapter)),
        )


def test_a_pattern_applied_to_a_non_string_is_a_violation_not_a_crash(store):
    adapter = _spec(schema={"operator.reply_style": {"pattern": "^[a-z]+$"}})
    facts = json.loads(json.dumps(FACTS))
    facts["operator"]["reply_style"] = 3
    with pytest.raises(GateFailure, match="needs text"):
        apply_consolidation(
            store, adapter, Proposal(facts=facts), session="s1", batch="b1",
            expected_fingerprint=UNCHECKED,
        )


def test_a_pattern_that_is_not_a_regex_is_refused_at_load():
    """Refused where the operator can still fix it, not from inside a gate mid-consolidation."""
    with pytest.raises(MementoError, match="not a valid regex"):
        _spec(schema={"operator.reply_style": {"pattern": "([unclosed"}})


def test_a_pattern_that_is_not_a_string_is_refused_at_load():
    with pytest.raises(MementoError, match="regex string"):
        _spec(schema={"operator.reply_style": {"pattern": ["^a$"]}})


def test_an_unknown_field_spec_key_is_refused():
    """`requried: true` would otherwise read as a declared gate and check nothing at all."""
    with pytest.raises(MementoError, match="unknown field spec key"):
        _spec(schema={"operator.reply_style": {"type": "str", "requried": True}})


def test_a_field_spec_may_not_smuggle_in_a_callable():
    with pytest.raises(MementoError, match="unknown field spec key"):
        _spec(schema={"operator.reply_style": {"check": "lambda v: True"}})


# ------------------------------------------------------------ tightened scale steps


def test_a_declared_step_of_zero_freezes_a_scale(store):
    """Tighter than the floor: the value may not move at all without an operator's own edit."""
    adapter = _spec(ordered_scale_steps={"operator.confidence": 0})
    _seed(store, adapter)

    moved = json.loads(json.dumps(FACTS))
    moved["operator"]["confidence"] = "high"
    with pytest.raises(GateFailure, match="no movement"):
        apply_consolidation(
            store, adapter, Proposal(facts=moved), session="s2", batch="b2",
            expected_fingerprint=facts_fingerprint(read_facts(store, adapter)),
        )


def test_the_same_move_is_allowed_without_the_tightening(store):
    """The control: one step is the floor's own limit, so freezing it has to be the *declaration*."""
    adapter = _spec()
    _seed(store, adapter)

    moved = json.loads(json.dumps(FACTS))
    moved["operator"]["confidence"] = "high"
    apply_consolidation(
        store, adapter, Proposal(facts=moved), session="s2", batch="b2",
        expected_fingerprint=facts_fingerprint(read_facts(store, adapter)),
    )
    assert read_facts(store, adapter)["operator"]["confidence"] == "high"


def test_a_step_above_the_floor_is_refused_at_load():
    with pytest.raises(MementoError, match="would loosen the floor"):
        _spec(ordered_scale_steps={"operator.confidence": 2})


def test_a_step_on_a_path_with_no_declared_scale_is_refused():
    """A limit on nothing reads as a gate and is one — refuse it rather than let it look present."""
    with pytest.raises(MementoError, match="no ordered scale is declared"):
        _spec(ordered_scale_steps={"operator.reply_style": 0})


def test_a_negative_step_is_refused():
    with pytest.raises(MementoError, match="cannot be negative"):
        _spec(ordered_scale_steps={"operator.confidence": -1})


def test_a_scale_that_repeats_a_value_is_refused():
    """`list.index` finds the first match, so a repeat measures a real jump as fewer steps."""
    with pytest.raises(MementoError, match="cannot be measured"):
        _spec(ordered_scales={"operator.confidence": ["low", "medium", "low"]})


def test_the_floor_clamps_a_library_caller_who_asks_for_more(store):
    """The spec loader is one door. A Python caller reaching past it hits the same limit.

    Two enforcement points on purpose: the loader gives the operator a readable refusal, and the
    clamp means the floor's guarantee does not depend on which door the adapter came through.
    """
    floor = OrderedScaleFloor(
        {"operator.confidence": ["low", "medium", "high"]},
        max_steps={"operator.confidence": 5},
    )
    assert floor.limit_for(("operator", "confidence")) == MAX_SCALE_STEP

    jumped = {"operator": {"confidence": "high"}}
    violations = floor.check(StoreState(facts={"operator": {"confidence": "low"}}), Proposal(facts=jumped))
    assert violations and "2 steps" in violations[0].detail


# --------------------------------------------------------------- required members


def test_a_required_member_may_not_be_dropped_even_with_a_tombstone(store):
    """Strictly tighter than the floor, which accepts any drop that is explicitly retired."""
    adapter = _spec(required_members={"practice": ["verify"]})
    _seed(store, adapter)

    without = {"operator": FACTS["operator"], "practice": [FACTS["practice"][1]]}
    with pytest.raises(GateFailure, match="required"):
        apply_consolidation(
            store, adapter, Proposal(facts=without, tombstones={"practice/verify"}),
            session="s2", batch="b2",
            expected_fingerprint=facts_fingerprint(read_facts(store, adapter)),
        )


def test_the_floor_alone_would_have_allowed_that_drop(store):
    """The control that makes the rule above a tightening rather than a restatement."""
    adapter = _spec()
    _seed(store, adapter)

    without = {"operator": FACTS["operator"], "practice": [FACTS["practice"][1]]}
    apply_consolidation(
        store, adapter, Proposal(facts=without, tombstones={"practice/verify"}),
        session="s2", batch="b2",
        expected_fingerprint=facts_fingerprint(read_facts(store, adapter)),
    )
    assert [p["topic"] for p in read_facts(store, adapter)["practice"]] == ["escalate"]


def test_a_required_collection_that_is_missing_entirely_is_reported(store):
    adapter = _spec(required_members={"practice": ["verify"]})
    with pytest.raises(GateFailure, match="practice"):
        apply_consolidation(
            store, adapter, Proposal(facts={"operator": FACTS["operator"]}),
            session="s1", batch="b1", expected_fingerprint=UNCHECKED,
        )


def test_an_empty_required_member_list_is_refused_at_load():
    with pytest.raises(MementoError, match="non-empty list"):
        _spec(required_members={"practice": []})


def test_required_member_ids_must_be_strings():
    with pytest.raises(MementoError, match="must be strings"):
        _spec(required_members={"practice": [3]})


# -------------------------------------------------------------- nothing loosened


def test_declaring_none_of_it_leaves_the_floor_exactly_as_it_was(store):
    """The empty case still fails closed — the property the whole vocabulary must not disturb."""
    adapter = _spec()
    _seed(store, adapter)

    eroded = {"operator": FACTS["operator"], "practice": FACTS["practice"][:1]}
    with pytest.raises(GateFailure, match="tombstone"):
        apply_consolidation(
            store, adapter, Proposal(facts=eroded), session="s2", batch="b2",
            expected_fingerprint=facts_fingerprint(read_facts(store, adapter)),
        )
