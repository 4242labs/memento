"""Status is never stored — it is folded from history at read time."""

from __future__ import annotations

import json

from memento.fold import ACTIVE, RETIRED, SUPERSEDED


def test_status_is_never_written_to_disk(store):
    store.append("errors/en", [{"id": "e1", "pattern": "x"}], session="s1", batch="b1")
    store.append(
        "errors/en", [{"id": "e1", "event": "retired", "reason": "fixed"}], session="s1", batch="b2"
    )
    for line in store.stream_path("errors/en").read_text().splitlines():
        assert "status" not in json.loads(line)

    assert store.folded("errors/en")["e1"].status == RETIRED


def test_supersession_is_an_event(store):
    store.append("errors/en", [{"id": "old", "pattern": "x"}], session="s1", batch="b1")
    store.append(
        "errors/en",
        [{"id": "old", "event": "superseded_by", "superseded_by": "new"}],
        session="s1",
        batch="b2",
    )
    folded = store.folded("errors/en")
    assert folded["old"].status == SUPERSEDED
    assert folded["old"].superseded_by == "new"


def test_a_tombstoned_entry_stays_visible_to_the_fold(store):
    """The anti-erosion gate needs to see the tombstone; a fold that hid it would reopen the hole."""
    store.append("errors/en", [{"id": "e1", "pattern": "x"}], session="s1", batch="b1")
    store.append("errors/en", [{"id": "e1", "event": "retired"}], session="s1", batch="b2")

    folded = store.folded("errors/en")
    assert "e1" in folded
    assert folded["e1"].is_tombstoned
    assert folded["e1"].payload["pattern"] == "x"


def test_a_re_observation_does_not_resurrect_a_tombstone(store):
    store.append("errors/en", [{"id": "e1", "pattern": "x"}], session="s1", batch="b1")
    store.append("errors/en", [{"id": "e1", "event": "retired"}], session="s1", batch="b2")
    store.append("errors/en", [{"id": "e1", "pattern": "x again"}], session="s2", batch="b3")

    assert store.folded("errors/en")["e1"].status == RETIRED


def test_contradiction_marks_the_entry_contested_without_rewriting_it(store):
    store.append("errors/en", [{"id": "e1", "pattern": "x"}], session="s1", batch="b1")
    store.append(
        "errors/en", [{"id": "e1", "event": "contradicted", "note": "learner disagreed"}],
        session="s2",
        batch="b2",
    )
    entry = store.folded("errors/en")["e1"]
    assert entry.contested and entry.status == ACTIVE and entry.payload["pattern"] == "x"


def test_repeat_observations_are_counted(store):
    for i in range(3):
        store.append("vocab/en", [{"id": "v1", "item": "word"}], session=f"s{i}", batch=f"b{i}")
    assert store.folded("vocab/en")["v1"].observations == 3
