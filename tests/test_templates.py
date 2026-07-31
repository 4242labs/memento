"""Engine prompt templates are SHA-pinned (ADR D1) — enforced, not documented."""

from __future__ import annotations

import pytest

from memento import MementoError
from memento import templates as tpl


def test_both_engine_templates_are_pinned():
    lock = tpl.read_lock()
    assert set(lock) == set(tpl.all_names()) == {tpl.RELATIONSHIP, tpl.RESTRAINT}


def test_the_pins_match_what_is_on_disk():
    assert tpl.compute_lock() == tpl.read_lock()


def test_loading_verifies_the_pin(tmp_path, monkeypatch):
    """A template edited without re-pinning must fail loudly, not ship quietly."""
    fake_dir = tmp_path / "prompts"
    fake_dir.mkdir()
    (fake_dir / "relationship.md").write_text("tampered\n", encoding="utf-8")
    monkeypatch.setattr(tpl, "TEMPLATE_DIR", fake_dir)

    with pytest.raises(MementoError, match="does not match its pin"):
        tpl.load(tpl.RELATIONSHIP)


def test_an_unknown_template_is_an_error():
    with pytest.raises(MementoError, match="no such template"):
        tpl.load("improvised")


def test_the_preamble_carries_both_and_is_stable():
    text = tpl.preamble()
    assert "Reference the past sparingly" in text
    assert "Record what was demonstrated, not what was agreeable" in text
    assert text == tpl.preamble()


def test_the_restraint_template_states_the_rules_the_gates_enforce():
    """The prompt and the gates must say the same thing, or the model is being set up to fail."""
    restraint = tpl.load(tpl.RESTRAINT)
    assert "Never drop what you cannot explain" in restraint
    assert "one step" in restraint
    assert "No secrets" in restraint
