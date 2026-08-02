"""Reading a store's documents back into facts, and refusing to when the bytes disagree (B-02 T5/AC-3).

`facts_from_store` was code-only, so a declared adapter had no way to get an anti-erosion baseline
out of a store that predates `.memento/facts.json` — and a baseline of `{}` cannot be eroded, which
makes the very first consolidation on an adopted store the least guarded one it will ever have.

The parser is type-directed because the renderer is lossy: booleans go out as `yes`/`no` and every
scalar goes out stringified, so no generic byte-level inverse exists. The types come from the spec's
declared `schema`, and the mapping-versus-list question — genuinely unanswerable from the markdown,
since both render as labelled bullets — comes from its declared `collections`.

Where the round-trip does not reproduce the bytes, **the bytes win**: nothing is rewritten, the
divergence is FLAGged, and adoption defers. Re-projecting an operator's own memory to match a
renderer is the operator's call.
"""

from __future__ import annotations

import json
import re

import pytest

from memento import MemoryStore, Proposal, adapter_from_spec, apply_consolidation, check_adoption
from memento.cli import main
from memento.errors import GateFailure
from memento.writepath import UNCHECKED, read_facts

SPEC = {
    "name": "adopted",
    "identity_keys": ["id", "topic", "name"],
    "documents": {
        "operator.md": {"title": "Operator", "sections": ["operator"]},
        "practice.md": {"title": "Practice", "sections": ["practice"]},
    },
    "schema": {
        "operator.confidence": {"type": "str", "enum": ["low", "medium", "high"]},
        "operator.sessions": {"type": "int"},
        "operator.wants_flags": {"type": "bool"},
        "operator.drift": {"type": "float"},
    },
    "collections": {"practice": {"kind": "list", "identity_key": "topic"}},
}

FACTS = {
    "operator": {
        "confidence": "medium",
        "sessions": 41,
        "wants_flags": True,
        "drift": 0.25,
        "timezone": "Europe/Lisbon",
    },
    "practice": [
        {"topic": "verify before asserting", "weight": "high", "note": "high"},
        {"topic": "own the mistake first", "weight": "medium"},
    ],
}


@pytest.fixture
def adapter():
    return adapter_from_spec(SPEC)


@pytest.fixture
def spec_file(tmp_path):
    path = tmp_path / "adapter.json"
    path.write_text(json.dumps(SPEC), encoding="utf-8")
    return str(path)


@pytest.fixture
def projected(tmp_path, adapter):
    """A store holding only the *documents* a declared adapter would have rendered.

    No `.memento/facts.json` — that absence is the whole scenario. This is what an adopted store
    looks like on the day the engine first opens it.
    """
    store = MemoryStore(tmp_path / "adopted")
    store.initialize()
    for name, text in adapter.render_documents(FACTS).items():
        store.replace_document(name, text, session="seed", batch="seed")
    return store


# ---------------------------------------------------------------- the round trip


def _canonical(facts):
    """Facts with list members in the renderer's own order.

    The renderer sorts members by identity, deliberately — the gates address them by identity and
    treat a reorder as a no-op, so rendering in input order would emit different bytes for facts the
    engine considers identical. The parse is therefore exact up to that ordering, and no further.
    """
    out = json.loads(json.dumps(facts))
    for key, value in out.items():
        if isinstance(value, list):
            out[key] = sorted(value, key=lambda m: m.get("topic", ""))
    return out


def test_the_parser_recovers_the_facts_the_renderer_was_given(projected, adapter):
    assert adapter.facts_from_store(projected) == _canonical(FACTS)


def test_the_recovered_facts_render_back_to_the_same_bytes(projected, adapter):
    """The adoption contract itself, stated as an assertion."""
    recovered = adapter.facts_from_store(projected)
    for name, text in adapter.render_documents(recovered).items():
        assert projected.read_document(name) == text


def test_types_survive_the_round_trip_because_the_spec_declared_them(projected, adapter):
    """The renderer stringifies everything, so this can only come from the declaration."""
    operator = adapter.facts_from_store(projected)["operator"]
    assert operator["sessions"] == 41 and isinstance(operator["sessions"], int)
    assert operator["wants_flags"] is True
    assert operator["drift"] == pytest.approx(0.25)
    assert operator["timezone"] == "Europe/Lisbon"  # undeclared stays text, and stays put


def test_a_member_field_equal_to_its_own_label_survives(projected, adapter):
    """`weight: high` alongside a `note: high` used to lose one of them.

    The renderer dropped every field whose *value* matched the member's label, having printed the
    label already. Dropping by value instead of by field name is lossy, and a lossy renderer has no
    inverse at all — so the fix is upstream of this test, in what the renderer emits.
    """
    member = next(p for p in adapter.facts_from_store(projected)["practice"] if p["topic"].startswith("verify"))
    assert member == {"topic": "verify before asserting", "weight": "high", "note": "high"}


def test_a_list_is_a_list_because_the_spec_said_so(projected, adapter):
    """A mapping and a list of identified members render identically. Only the spec can tell them apart."""
    recovered = adapter.facts_from_store(projected)
    assert isinstance(recovered["practice"], list)
    assert isinstance(recovered["operator"], dict)


def test_an_undeclared_collection_reads_back_as_a_mapping(tmp_path):
    """The renderer's own default. Getting a list back would require a declaration that is absent."""
    adapter = adapter_from_spec({**SPEC, "collections": {}})
    store = MemoryStore(tmp_path / "s")
    store.initialize()
    for name, text in adapter.render_documents(FACTS).items():
        store.replace_document(name, text, session="seed", batch="seed")
    assert isinstance(adapter.facts_from_store(store)["practice"], dict)


def test_a_list_collection_must_name_its_identity_key():
    with pytest.raises(Exception, match="identity_key"):
        adapter_from_spec({**SPEC, "collections": {"practice": {"kind": "list"}}})


def test_an_identity_key_the_floor_cannot_address_is_refused():
    """Facts the floor cannot verify are worse than facts it never saw."""
    with pytest.raises(Exception, match="identity_keys"):
        adapter_from_spec(
            {**SPEC, "collections": {"practice": {"kind": "list", "identity_key": "slug"}}}
        )


# ------------------------------------------------- what the baseline is actually for


def test_the_first_consolidation_on_an_adopted_store_already_has_a_baseline(projected, adapter):
    """Without the parser this proposal lands: an empty baseline has nothing to erode."""
    eroded = json.loads(json.dumps(FACTS))
    del eroded["practice"][1]

    with pytest.raises(GateFailure, match="own the mistake first"):
        apply_consolidation(
            projected, adapter, Proposal(facts=eroded), session="s1", batch="b1",
            expected_fingerprint=UNCHECKED,
        )


def test_read_facts_prefers_the_documents_when_there_is_no_facts_json(projected, adapter):
    assert read_facts(projected, adapter)["operator"]["timezone"] == "Europe/Lisbon"


# ------------------------------------------------------------------ bytes win


JUBS_PROFILE = """\
## fr
- level: B1
- confidence: medium
"""


@pytest.fixture
def jubs_layout(tmp_path):
    """The B-01 compatibility target: hand-written markdown this renderer does not produce.

    Every byte is synthetic. The layout is jubs'; no operator content is.
    """
    store = MemoryStore(tmp_path / "jubs")
    store.initialize()
    store.replace_document("operator.md", JUBS_PROFILE, session="seed", batch="seed")
    return store


def test_a_store_this_renderer_cannot_reproduce_is_flagged_not_rewritten(jubs_layout, adapter):
    report = check_adoption(jubs_layout, adapter)

    assert not report.ok
    assert "operator.md" in report.diverged
    assert report.flags and report.flags[0].kind == "adoption-diverged"
    assert jubs_layout.read_document("operator.md") == JUBS_PROFILE, "the store's bytes must be untouched"


def test_facts_from_store_on_the_cli_refuses_a_divergent_store(jubs_layout, spec_file, capsys):
    """Asserted on the contract — exit code and `--json` payload — not on the console prose."""
    code = main([
        "--store", str(jubs_layout.root), "facts", "--from-store",
        "--adapter-file", spec_file, "--json",
    ])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["ok"] is False
    assert payload["diverged"] == ["operator.md"]
    assert any("adoption-diverged" in flag for flag in payload["flags"])


def test_facts_from_store_on_the_cli_prints_the_facts_when_the_bytes_agree(projected, spec_file, capsys):
    code = main(["--store", str(projected.root), "facts", "--from-store", "--adapter-file", spec_file])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["operator"]["sessions"] == 41


def test_a_document_the_store_does_not_have_yet_is_not_a_divergence(tmp_path, adapter):
    """An empty or partial store is the ordinary case, not a refusal."""
    store = MemoryStore(tmp_path / "fresh")
    store.initialize()
    report = check_adoption(store, adapter)
    assert report.ok


def test_prose_a_consumer_added_by_hand_does_not_become_facts(projected, adapter):
    """A document the engine projects is still a file someone may have written in.

    Non-bullet lines are left where they are rather than parsed into facts — which then shows up
    honestly as a divergence, because re-rendering would drop them.
    """
    text = (projected.read_document("operator.md") or "") + "\nA note the operator typed.\n"
    projected.replace_document("operator.md", text, session="s", batch="b")

    recovered = adapter.facts_from_store(projected)
    assert not any("typed" in str(v) for v in recovered["operator"].values())
    assert not check_adoption(projected, adapter).ok


def test_from_store_says_so_when_the_adapter_cannot_parse_documents(projected, capsys):
    """`{}` and exit 0 would read as "the store is empty". An empty baseline is one free erosion."""
    code = main([
        "--store", str(projected.root), "facts", "--from-store",
        "--adapter", "fixture_consumer:ADAPTER",
    ])
    assert code == 1
    assert "cannot parse documents back into facts" in capsys.readouterr().err


def test_an_unlabelled_member_keeps_its_body(tmp_path, adapter):
    """A member with no identity the engine recognises still must not read back as nothing.

    The floor refuses to *write* one — `membership()` reports it and the proposal is rejected — so
    this can only arrive in a document written by something else. Adoption is precisely the case
    where "something else wrote it" is the premise, and dropping the body would be a silent loss
    where a divergence FLAG was the whole promise.
    """
    from memento.spec import facts_from_documents

    store = MemoryStore(tmp_path / "unlabelled")
    store.initialize()
    store.replace_document(
        "practice.md",
        "# Practice\n\n## practice\n-\n  - **weight**: high\n",
        session="seed",
        batch="seed",
    )

    recovered = facts_from_documents(
        store,
        {"practice.md": {"sections": ["practice"]}},
        schema={},
        collections={"practice": {"kind": "list", "identity_key": "topic"}},
    )
    assert recovered["practice"] == [{"weight": "high"}]


def test_a_parser_that_recovers_nothing_is_refused_rather_than_believed(tmp_path):
    """The adversarial review's CRITICAL-2. Unverifiable must fail closed, as the floor does.

    A Python-authored adapter leaves `projected_documents` empty — the adapter contract's own
    minimum example never sets it — so the comparison below had nothing to compare and reported
    `ok=True` with `facts={}`. That is precisely the "one free erosion" this module exists to
    prevent: the first consolidation would be judged against an empty baseline, and an empty
    baseline cannot be eroded.
    """
    from memento import Adapter

    store = MemoryStore(tmp_path / "python-adapter")
    store.initialize()
    store.replace_document("profile.md", "# Profile\n\n- name: Alice\n", session="s", batch="b")

    blind = Adapter(
        name="python-authored",
        render_documents=lambda facts: {"profile.md": "..."} if facts.get("profile") else {},
        facts_from_store=lambda store: {},  # recovers nothing at all
    )

    report = check_adoption(store, blind)
    assert not report.ok
    assert report.unverified == ("profile.md",)
    assert report.facts == {}
    assert "one free erosion" in (report.message() or "")
    assert store.read_document("profile.md") == "# Profile\n\n- name: Alice\n"


def test_an_empty_store_with_an_empty_parse_is_still_fine(tmp_path):
    """The control: nothing on disk means nothing to fail to verify."""
    from memento import Adapter

    store = MemoryStore(tmp_path / "empty")
    store.initialize()
    blind = Adapter(name="x", render_documents=lambda f: {}, facts_from_store=lambda s: {})

    assert check_adoption(store, blind).ok
