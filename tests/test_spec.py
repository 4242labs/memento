"""Declared adapters, and the CLI a consumer with no Python of its own has to use.

The engine's second consumer class is an agent: markdown and a shell, no import statement anywhere.
These tests exist to prove that going through a spec file and a subprocess does not buy a consumer
a weaker engine — the floor, the secrets gate and the compare-and-swap all still apply, and the
renderer the engine supplies in place of the consumer's own is genuinely deterministic.
"""

from __future__ import annotations

import json

import pytest

from memento import MemoryStore, adapter_from_spec, apply_consolidation, load_adapter
from memento.cli import main
from memento.errors import MementoError
from memento.gates import Proposal
from memento.writepath import UNCHECKED, facts_fingerprint, read_facts

SPEC = {
    "name": "fixture-mode",
    "prefix_budget_tokens": 400,
    "identity_keys": ["id", "topic", "name", "key"],
    "documents": {
        "profile.md": {"title": "Operator", "sections": ["operator"]},
        "practice.md": {"title": "Practice", "sections": ["practice"]},
    },
    "prefix_sections": [
        {"name": "profile", "priority": 0, "document": "profile.md"},
        {"name": "practice", "priority": 1, "document": "practice.md"},
    ],
    "schema": {"operator.confidence": {"type": "str", "enum": ["low", "medium", "high"]}},
    "ordered_scales": {"operator.confidence": ["low", "medium", "high"]},
    "retention": {"keep_everything": True},
    "distillation_prompt": "(fixture)",
}

FACTS = {
    "operator": {"confidence": "medium", "timezone": "Europe/Lisbon"},
    "practice": [
        {"topic": "terse replies", "weight": "high"},
        {"topic": "no narration", "weight": "high"},
    ],
}


@pytest.fixture
def spec_file(tmp_path):
    path = tmp_path / "adapter.json"
    path.write_text(json.dumps(SPEC), encoding="utf-8")
    return path


@pytest.fixture
def declared(spec_file):
    return load_adapter(spec_file)


# ------------------------------------------------------------------ the renderer


def test_renderer_is_a_pure_function_of_content_not_insertion_order(declared):
    """Same facts, same bytes — whatever order the keys were built in.

    `render_documents` must be deterministic (docs/adapter-contract.md). A renderer that walked a
    dict in insertion order would emit different bytes for facts the gates consider identical, so
    every consolidation would append a `document_replaced` event and the history would be noise.
    """
    shuffled = {
        "practice": list(reversed(FACTS["practice"])),
        "operator": {"timezone": "Europe/Lisbon", "confidence": "medium"},
    }
    assert declared.render_documents(FACTS) == declared.render_documents(shuffled)


def test_renderer_omits_documents_whose_sections_are_absent(declared):
    rendered = declared.render_documents({"operator": {"confidence": "low"}})
    assert "profile.md" in rendered
    assert "practice.md" not in rendered


def test_rendered_document_carries_the_content(declared):
    rendered = declared.render_documents(FACTS)
    assert "Europe/Lisbon" in rendered["profile.md"]
    assert "terse replies" in rendered["practice.md"]


# --------------------------------------------------------------- spec validation


def test_unknown_spec_key_is_refused(tmp_path):
    """A typo must not silently disable the gate it was meant to declare."""
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"name": "x", "ordered_scale": {}}), encoding="utf-8")
    with pytest.raises(MementoError, match="unknown spec key"):
        load_adapter(path)


def test_unknown_field_type_is_refused():
    with pytest.raises(MementoError, match="unknown type"):
        adapter_from_spec({"name": "x", "schema": {"a.b": {"type": "string"}}})


def test_spec_without_a_name_is_refused():
    with pytest.raises(MementoError, match="must declare a name"):
        adapter_from_spec({})


def test_prefix_section_without_a_document_is_refused():
    with pytest.raises(MementoError, match="must name a document"):
        adapter_from_spec({"name": "x", "prefix_sections": [{"name": "p", "priority": 0}]})


# ------------------------------------------------------------------- the floor


def test_a_declared_adapter_still_gets_the_anti_erosion_floor(store, declared):
    """The whole point: declaring an adapter in JSON must not buy a weaker engine.

    A spec cannot ship a custom `Rule`, so if the floor did not compose in automatically a declared
    consumer would have *no* write discipline at all — which is exactly the fail-open the ADR's
    "empty adapter rule set fails closed" clause exists to prevent.
    """
    apply_consolidation(
        store, declared, Proposal(facts=FACTS), session="s1", batch="b1",
        expected_fingerprint=UNCHECKED,
    )
    eroded = {"operator": FACTS["operator"], "practice": FACTS["practice"][:1]}
    with pytest.raises(Exception) as exc:
        apply_consolidation(
            store, declared, Proposal(facts=eroded), session="s2", batch="b2",
            expected_fingerprint=facts_fingerprint(read_facts(store, declared)),
        )
    assert "tombstone" in str(exc.value)


def test_a_declared_ordered_scale_still_bounds_the_step(store, declared):
    apply_consolidation(
        store, declared, Proposal(facts=FACTS), session="s1", batch="b1",
        expected_fingerprint=UNCHECKED,
    )
    jumped = json.loads(json.dumps(FACTS))
    jumped["operator"]["confidence"] = "low"  # medium -> low is one step: allowed
    apply_consolidation(
        store, declared, Proposal(facts=jumped), session="s2", batch="b2",
        expected_fingerprint=facts_fingerprint(read_facts(store, declared)),
    )
    two_steps = json.loads(json.dumps(jumped))
    two_steps["operator"]["confidence"] = "high"  # low -> high is two
    with pytest.raises(Exception, match="steps"):
        apply_consolidation(
            store, declared, Proposal(facts=two_steps), session="s3", batch="b3",
            expected_fingerprint=facts_fingerprint(read_facts(store, declared)),
        )


# ---------------------------------------------------------------------- the CLI


def _proposal_file(tmp_path, facts, name="proposal.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"facts": facts}), encoding="utf-8")
    return str(path)


def test_cli_consolidate_writes_through_the_gates(tmp_path, spec_file, capsys):
    root = tmp_path / "store"
    assert main(["--store", str(root), "consolidate", "--adapter-file", str(spec_file),
                 "--proposal", _proposal_file(tmp_path, FACTS), "--session", "s1",
                 "--unchecked"]) == 0
    store = MemoryStore(root)
    assert "Europe/Lisbon" in (store.read_document("profile.md") or "")


def test_cli_consolidate_reports_every_violation_and_writes_nothing(tmp_path, spec_file, capsys):
    root = tmp_path / "store"
    main(["--store", str(root), "consolidate", "--adapter-file", str(spec_file),
          "--proposal", _proposal_file(tmp_path, FACTS), "--session", "s1", "--unchecked"])
    before = MemoryStore(root).read_document("practice.md")

    eroded = {"operator": FACTS["operator"], "practice": []}
    code = main(["--store", str(root), "consolidate", "--adapter-file", str(spec_file),
                 "--proposal", _proposal_file(tmp_path, eroded, "p2.json"), "--session", "s2",
                 "--unchecked"])
    assert code == 3
    assert "REJECTED" in capsys.readouterr().err
    assert MemoryStore(root).read_document("practice.md") == before


def test_cli_consolidate_refuses_a_stale_proposal(tmp_path, spec_file, capsys):
    """The compare-and-swap survives the shell boundary.

    An agent reads `facts --fingerprint`, thinks, and submits later. If another writer landed in
    between, the write must be refused rather than silently overwriting them.
    """
    root = tmp_path / "store"
    main(["--store", str(root), "consolidate", "--adapter-file", str(spec_file),
          "--proposal", _proposal_file(tmp_path, FACTS), "--session", "s1", "--unchecked"])
    stale = facts_fingerprint({})
    code = main(["--store", str(root), "consolidate", "--adapter-file", str(spec_file),
                 "--proposal", _proposal_file(tmp_path, FACTS, "p2.json"), "--session", "s2",
                 "--expect", stale])
    assert code == 5
    assert "REJECTED" in capsys.readouterr().err


def test_cli_fingerprint_round_trips(tmp_path, spec_file, capsys):
    root = tmp_path / "store"
    main(["--store", str(root), "consolidate", "--adapter-file", str(spec_file),
          "--proposal", _proposal_file(tmp_path, FACTS), "--session", "s1", "--unchecked"])
    capsys.readouterr()
    assert main(["--store", str(root), "facts", "--fingerprint",
                 "--adapter-file", str(spec_file)]) == 0
    fingerprint = capsys.readouterr().out.strip()

    grown = json.loads(json.dumps(FACTS))
    grown["practice"].append({"topic": "verify before asserting", "weight": "high"})
    assert main(["--store", str(root), "consolidate", "--adapter-file", str(spec_file),
                 "--proposal", _proposal_file(tmp_path, grown, "p2.json"), "--session", "s2",
                 "--expect", fingerprint]) == 0


def test_cli_consolidate_rejects_secrets(tmp_path, spec_file, capsys):
    from support.fake_credentials import fake_credential

    root = tmp_path / "store"
    poisoned = json.loads(json.dumps(FACTS))
    poisoned["operator"]["note"] = fake_credential("aws-access-key")
    code = main(["--store", str(root), "consolidate", "--adapter-file", str(spec_file),
                 "--proposal", _proposal_file(tmp_path, poisoned), "--session", "s1",
                 "--unchecked"])
    assert code == 4
    assert not (root / "profile.md").exists()


def test_cli_prefix_is_budgeted_and_local(tmp_path, spec_file, capsys):
    root = tmp_path / "store"
    main(["--store", str(root), "consolidate", "--adapter-file", str(spec_file),
          "--proposal", _proposal_file(tmp_path, FACTS), "--session", "s1", "--unchecked"])
    capsys.readouterr()
    assert main(["--store", str(root), "prefix", "--adapter-file", str(spec_file), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counter"] == "memento.heuristic.v1"
    assert payload["tokens"] <= payload["budget"]
    assert "Europe/Lisbon" in payload["text"]


def test_cli_prefix_truncation_is_reported_not_silent(tmp_path, spec_file, capsys):
    root = tmp_path / "store"
    main(["--store", str(root), "consolidate", "--adapter-file", str(spec_file),
          "--proposal", _proposal_file(tmp_path, FACTS), "--session", "s1", "--unchecked"])
    capsys.readouterr()
    main(["--store", str(root), "prefix", "--adapter-file", str(spec_file),
          "--budget", "5", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["truncated"] or payload["dropped"]
    assert payload["flags"]


def test_cli_command_needing_an_adapter_says_so(tmp_path, capsys):
    root = tmp_path / "store"
    assert main(["--store", str(root), "prefix"]) == 1
    assert "adapter" in capsys.readouterr().err
