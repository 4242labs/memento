"""Fixtures for the deterministic tier (ADR D6).

Everything here is **synthetic**. No real operator data enters this repo — the corpus is invented,
and the distiller is a stub returning recorded output, so the whole tier runs with no model, no
credentials, and no token spend. That is the point: the LLM call is injected precisely so write
discipline is testable without one.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from memento.writepath import UNCHECKED
from memento import (
    Adapter,
    FieldSpec,
    FrozenClock,
    MemoryStore,
    PrefixSection,
    Proposal,
    Queue,
    RetentionPolicy,
    StoreState,
)

LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]
CONFIDENCE = ["low", "medium", "high"]
ENGAGEMENT = ["low", "medium", "high"]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def render_documents(facts: dict[str, Any]) -> dict[str, str]:
    languages = facts.get("languages", {})
    lines: list[str] = []
    for code in sorted(languages):
        lines.append(f"## {code}")
        for key in sorted(languages[code]):
            lines.append(f"- {key}: {languages[code][key]}")
        lines.append("")
    profile = ("\n".join(lines).strip() or "(no languages recorded)") + "\n"

    rows = ["| topic | engagement | notes |", "|:--|:--|:--|"]
    for item in sorted(facts.get("interests", []), key=lambda i: i["topic"]):
        rows.append(f"| {item['topic']} | {item['engagement']} | {item.get('notes', '')} |")
    return {"profile.md": profile, "interests.md": "\n".join(rows) + "\n"}


def derive_entry_id(stream: str, entry: dict[str, Any]) -> str | None:
    """Errors are identified by language + pattern, so a reworded note cannot fork an entry."""
    if not stream.startswith("errors/"):
        return None
    if "pattern" not in entry:
        return None
    return f"{stream.split('/')[-1]}-{slug(str(entry['pattern']))}"


def _recent_errors(store: MemoryStore, limit: int = 5) -> str:
    out = []
    for stream in sorted(s for s in store.streams() if s.startswith("errors/")):
        for entry_id, entry in store.folded(stream).items():
            if entry.is_active:
                out.append(f"- {entry_id}: {entry.payload.get('pattern', '')}")
    return "\n".join(sorted(out)[:limit])


def make_adapter(**overrides: Any) -> Adapter:
    base = dict(
        name="fixture-linguist",
        prefix_budget_tokens=200,
        prefix_sections=(
            PrefixSection(
                name="profile",
                priority=0,
                required=False,
                render=lambda s: s.read_document("profile.md") or "",
            ),
            PrefixSection(
                name="interests", priority=1, render=lambda s: s.read_document("interests.md") or ""
            ),
            PrefixSection(name="recent-errors", priority=2, render=_recent_errors),
        ),
        schema={
            "languages.*.level": FieldSpec(type=str, enum=LEVELS),
            "languages.*.confidence": FieldSpec(type=str, enum=CONFIDENCE),
            "interests.*.engagement": FieldSpec(type=str, enum=ENGAGEMENT),
        },
        entry_schema={"id": FieldSpec(type=str, required=True)},
        ordered_scales={
            "languages.*.level": LEVELS,
            "languages.*.confidence": CONFIDENCE,
            "interests.*.engagement": ENGAGEMENT,
        },
        derive_entry_id=derive_entry_id,
        render_documents=render_documents,
        retention=RetentionPolicy(keep_everything=True),
        distillation_prompt="(fixture distillation prompt)",
    )
    base.update(overrides)
    return Adapter(**base)


def base_facts() -> dict[str, Any]:
    """Invented, and deliberately unlike any real store. See the module docstring."""
    return {
        "languages": {
            "fr": {"level": "B1", "confidence": "medium", "goals": "read philosophy unaided"},
            "de": {"level": "A1", "confidence": "low", "goals": "order breakfast without switching"},
        },
        "interests": [
            {"topic": "kite building", "engagement": "medium", "notes": "builds delta kites"},
            {"topic": "lighthouses", "engagement": "low", "notes": "mentioned once"},
        ],
    }


@dataclass
class StubDistiller:
    """A recorded distiller. Counts its calls, so 'never double-paid' is an assertion, not a hope."""

    proposal: Proposal | None = None
    error: Exception | None = None
    calls: int = 0
    seen_states: list[StoreState] = field(default_factory=list)
    on_call: Any = None

    def distill(self, journal: list[dict[str, Any]], state: StoreState, prompt: str) -> Proposal:
        self.calls += 1
        self.seen_states.append(state)
        if self.on_call is not None:
            self.on_call(journal, state)
        if self.error is not None:
            raise self.error
        assert self.proposal is not None, "stub distiller needs a proposal or an error"
        return copy.deepcopy(self.proposal)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def store(tmp_path, clock) -> MemoryStore:
    store = MemoryStore(tmp_path / "memory", clock=clock)
    store.initialize()
    return store


@pytest.fixture
def queue(tmp_path, clock) -> Queue:
    return Queue(tmp_path / "sessions-data", clock=clock)


@pytest.fixture
def adapter() -> Adapter:
    return make_adapter()


@pytest.fixture
def seeded(store, adapter):
    """A store carrying one consolidation, so later proposals have something to erode."""
    from memento import apply_consolidation

    apply_consolidation(
        store,
        adapter,
        Proposal(
            facts=base_facts(),
            entries={
                "errors/fr": [
                    {"id": "fr-je-suis-20-ans", "pattern": "je suis 20 ans", "should_be": "j'ai 20 ans"}
                ]
            },
            session_log="synthetic session log",
        ),
        session="s1",
        batch="b1",
        expected_fingerprint=UNCHECKED,
    )
    return store


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "acceptance: end-to-end, subprocess-driven. Proves the contract composes across real "
        "process boundaries; far too slow to be a per-mutant runner (B-02 T8 R3).",
    )


def pytest_collection_modifyitems(config, items):
    """Drop the acceptance tier when a mutation run asks for it.

    `MEMENTO_MUTATION=1 uv run mutmut run` is the documented invocation. Every mutant otherwise pays
    for the subprocess harness — dozens of interpreter starts per mutant — which took the sweep from
    minutes to hours and measured nothing the in-process tests do not already measure. The
    acceptance tier still runs in CI and in a plain `pytest`, which is where it belongs.
    """
    if not os.environ.get("MEMENTO_MUTATION"):
        return
    skip = pytest.mark.skip(reason="acceptance tier: excluded from the mutation runner (T8 R3)")
    for item in items:
        if "acceptance" in item.keywords:
            item.add_marker(skip)
