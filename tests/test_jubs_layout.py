"""Zero-layout-migration against the compatibility target (ADR D2, AC-2).

jubs' store is the shape the engine must adopt without converting anything: projected `profile.md`
and `interests.md` at the root, per-stream JSONL event logs in `errors/` and `vocab/`, free-prose
session logs in `sessions/`. Phase B moves that store's *path* and nothing else.

Every byte here is synthetic. The layout is copied; no operator content is.
"""

from __future__ import annotations

import json
import re

import pytest

from memento.writepath import UNCHECKED
from memento import Adapter, FieldSpec, GateFailure, MemoryStore, Proposal, apply_consolidation
from memento.store import ENGINE_DIR

LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]

PROFILE = """\
## fr
- level: B1
- confidence: medium
- evidence_date: 2026-07-31
- goals: follow a podcast without subtitles

## pt
- level: A1
- confidence: low
- evidence_date: 2026-07-31
- goals: read song lyrics
"""

INTERESTS = """\
| topic | last_discussed | engagement | notes |
|:--|:--|:--|:--|
| kite building | 2026-07-31 | medium | builds delta kites |
| lighthouses | 2026-07-31 | low | mentioned once |
"""

ERRORS_FR = [
    {
        "id": "fr-je-suis-20-ans",
        "event": "entry",
        "ts": "2026-07-31T13:54:32Z",
        "session": "260731-135432",
        "batch": "consolidation",
        "ordinal": 0,
        "pattern": "je suis 20 ans",
        "should_be": "j'ai 20 ans",
        "user_said": "je suis 20 ans",
        "type": "grammar",
        "note": "age uses 'avoir' in French",
    }
]

VOCAB_FR = [
    {
        "id": "fr-cerf-volant",
        "event": "entry",
        "ts": "2026-07-31T13:56:00Z",
        "session": "260731-135432",
        "batch": "consolidation",
        "ordinal": 0,
        "item": "cerf-volant",
        "context": "talking about kites",
    }
]


@pytest.fixture
def jubs_store(tmp_path):
    """A store laid out exactly like today's jubs-memory, with invented contents."""
    root = tmp_path / "jubs-memory"
    (root / "errors").mkdir(parents=True)
    (root / "vocab").mkdir(parents=True)
    (root / "sessions").mkdir(parents=True)

    (root / "README.md").write_text("# memory\n", encoding="utf-8")
    (root / "profile.md").write_text(PROFILE, encoding="utf-8")
    (root / "interests.md").write_text(INTERESTS, encoding="utf-8")
    (root / "sessions" / "log-260731-135432.md").write_text(
        "Learner talked about kites and coastal navigation.\n", encoding="utf-8"
    )
    for name, rows in (("errors/fr", ERRORS_FR), ("vocab/fr", VOCAB_FR)):
        (root / f"{name}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
    (root / "errors" / "pt.jsonl").write_text("", encoding="utf-8")
    return root


def _tree(root):
    return {
        str(p.relative_to(root)): p.stat().st_size
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ------------------------------------------------------- reading, unmodified


def test_the_store_opens_with_no_migration_step(jubs_store):
    store = MemoryStore(jubs_store)
    before = _tree(jubs_store)

    assert set(store.streams()) == {"errors/fr", "errors/pt", "vocab/fr"}
    assert store.documents() == ["interests.md", "profile.md"]
    assert store.session_logs() == ["log-260731-135432.md"]
    assert _tree(jubs_store) == before  # reading changed nothing at all


def test_existing_events_round_trip_field_for_field(jubs_store):
    store = MemoryStore(jubs_store)
    (event,) = store.log("errors/fr").read()
    assert event.to_obj() == ERRORS_FR[0]
    assert event.turn is None  # the one added stamp stays absent on pre-engine events


def test_existing_events_fold(jubs_store):
    store = MemoryStore(jubs_store)
    folded = store.folded("errors/fr")
    assert folded["fr-je-suis-20-ans"].payload["should_be"] == "j'ai 20 ans"
    assert folded["fr-je-suis-20-ans"].is_active


def test_an_empty_stream_file_is_not_a_corrupt_one(jubs_store):
    assert MemoryStore(jubs_store).log("errors/pt").read() == []


# ------------------------------------------------------ writing, still no migration


def _parse_profile(text: str) -> dict:
    languages: dict = {}
    current = None
    for line in text.splitlines():
        heading = re.match(r"^##\s+(\w+)$", line)
        if heading:
            current = heading.group(1)
            languages[current] = {}
        elif current and line.startswith("- "):
            key, _, value = line[2:].partition(": ")
            languages[current][key] = value
    return languages


def _parse_interests(text: str) -> list[dict]:
    out = []
    for line in text.splitlines()[2:]:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4:
            out.append(
                {"topic": cells[0], "last_discussed": cells[1], "engagement": cells[2], "notes": cells[3]}
            )
    return out


def _render(facts: dict) -> dict[str, str]:
    blocks = []
    for code in sorted(facts.get("languages", {})):
        fields = facts["languages"][code]
        body = "\n".join(f"- {k}: {fields[k]}" for k in ("level", "confidence", "evidence_date", "goals") if k in fields)
        blocks.append(f"## {code}\n{body}")
    rows = ["| topic | last_discussed | engagement | notes |", "|:--|:--|:--|:--|"]
    for item in sorted(facts.get("interests", []), key=lambda i: i["topic"]):
        rows.append(
            f"| {item['topic']} | {item['last_discussed']} | {item['engagement']} | {item['notes']} |"
        )
    return {"profile.md": "\n\n".join(blocks) + "\n", "interests.md": "\n".join(rows) + "\n"}


def _jubs_adapter() -> Adapter:
    return Adapter(
        name="jubs-compat",
        schema={"languages.*.level": FieldSpec(type=str, enum=LEVELS)},
        ordered_scales={"languages.*.level": LEVELS},
        render_documents=_render,
        facts_from_store=lambda store: {
            "languages": _parse_profile(store.read_document("profile.md") or ""),
            "interests": _parse_interests(store.read_document("interests.md") or ""),
        },
    )


def test_the_documents_survive_a_parse_render_round_trip(jubs_store):
    """If the engine's rendering did not reproduce the layout, adoption would be a migration."""
    store = MemoryStore(jubs_store)
    adapter = _jubs_adapter()
    facts = adapter.facts_from_store(store)
    rendered = _render(facts)

    assert rendered["profile.md"] == PROFILE
    assert rendered["interests.md"] == INTERESTS


def test_the_very_first_consolidation_already_has_an_anti_erosion_baseline(jubs_store):
    """No `.memento/facts.json` yet, so the baseline comes from the documents themselves."""
    store = MemoryStore(jubs_store)
    adapter = _jubs_adapter()
    facts = adapter.facts_from_store(store)
    del facts["languages"]["pt"]

    with pytest.raises(GateFailure, match="languages/pt"):
        apply_consolidation(store, adapter, Proposal(facts=facts), session="s1", batch="b1", expected_fingerprint=UNCHECKED)


def test_consolidating_adds_only_the_engine_area(jubs_store):
    store = MemoryStore(jubs_store)
    adapter = _jubs_adapter()
    before = _tree(jubs_store)

    facts = adapter.facts_from_store(store)
    facts["interests"].append(
        {"topic": "meteor showers", "last_discussed": "2026-08-01", "engagement": "low", "notes": "new"}
    )
    apply_consolidation(
        store,
        adapter,
        Proposal(facts=facts, entries={"errors/pt": [{"id": "pt-eu-tenho", "pattern": "eu tem 20 anos"}]}),
        session="260801-090000",
        batch="consolidation",
        expected_fingerprint=UNCHECKED,
    )

    after = _tree(jubs_store)
    assert set(before) <= set(after), "a pre-existing file was removed or renamed"

    added = set(after) - set(before)
    assert all(
        p.startswith(f"{ENGINE_DIR}/") or p.startswith("sessions/") for p in added
    ), f"unexpected additions outside the engine area: {sorted(added)}"


def test_untouched_streams_are_left_byte_identical(jubs_store):
    store = MemoryStore(jubs_store)
    adapter = _jubs_adapter()
    original = (jubs_store / "errors" / "fr.jsonl").read_bytes()

    apply_consolidation(
        store,
        adapter,
        Proposal(facts=adapter.facts_from_store(store)),
        session="260801-090000",
        batch="consolidation",
        expected_fingerprint=UNCHECKED,
    )

    assert (jubs_store / "errors" / "fr.jsonl").read_bytes() == original


def test_appending_to_an_existing_stream_preserves_what_was_there(jubs_store):
    store = MemoryStore(jubs_store)
    store.append("errors/fr", [{"id": "fr-new", "pattern": "new one"}], session="s2", batch="b1")

    events = store.log("errors/fr").read()
    assert [e.id for e in events] == ["fr-je-suis-20-ans", "fr-new"]
    assert events[0].to_obj() == ERRORS_FR[0]  # the pre-existing line is untouched


def test_a_replaced_document_keeps_its_prior_content_in_the_history(jubs_store):
    store = MemoryStore(jubs_store)
    adapter = _jubs_adapter()
    facts = adapter.facts_from_store(store)
    facts["languages"]["fr"]["level"] = "B2"

    apply_consolidation(store, adapter, Proposal(facts=facts), session="s2", batch="b1", expected_fingerprint=UNCHECKED)

    history = store.document_history("profile.md")
    assert history[-1].payload["prior_content"] == PROFILE
