"""Event log: stamps, idempotent batch append, and what a crash mid-append may leave behind."""

from __future__ import annotations

import json

import pytest

from memento import CorruptStoreError
from memento.events import Event, EventLog


def test_every_event_carries_the_full_stamp(store):
    store.append("errors/en", [{"id": "e1", "pattern": "x"}], session="s1", batch="b1", turn=3)
    (event,) = store.log("errors/en").read()
    assert (event.session, event.batch, event.ordinal, event.turn) == ("s1", "b1", 0, 3)
    assert event.ts and event.id == "e1"


def test_turn_is_omitted_when_unset(store):
    """Pre-engine logs have no `turn`; writing one back must not invent the field."""
    store.append("errors/en", [{"id": "e1", "pattern": "x"}], session="s1", batch="b1")
    line = store.stream_path("errors/en").read_text().strip()
    assert "turn" not in json.loads(line)


def test_ordinals_are_assigned_by_position(store):
    store.append(
        "vocab/en",
        [{"id": "a", "item": "one"}, {"id": "b", "item": "two"}, {"id": "c", "item": "three"}],
        session="s1",
        batch="b1",
    )
    assert [e.ordinal for e in store.log("vocab/en").read()] == [0, 1, 2]


def test_batch_append_is_idempotent(store):
    entries = [{"id": "e1", "pattern": "x"}, {"id": "e2", "pattern": "y"}]
    store.append("errors/en", entries, session="s1", batch="b1")
    store.append("errors/en", entries, session="s1", batch="b1")
    store.append("errors/en", entries, session="s1", batch="b1")
    assert len(store.log("errors/en").read()) == 2


def test_a_different_batch_is_not_deduped(store):
    entry = [{"id": "e1", "pattern": "x"}]
    store.append("errors/en", entry, session="s1", batch="b1")
    store.append("errors/en", entry, session="s1", batch="b2")
    assert len(store.log("errors/en").read()) == 2


def test_a_torn_trailing_line_is_tolerated_and_replayed(store):
    """The only residue a crash mid-append can leave. The batch is replayed, nothing is lost."""
    store.append("errors/en", [{"id": "e1", "pattern": "x"}], session="s1", batch="b1")
    path = store.stream_path("errors/en")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"id": "e2", "event": "entry", "ts": "2026-')  # power cut, mid-line

    assert [e.id for e in store.log("errors/en").read()] == ["e1"]


def test_a_corrupt_line_in_the_middle_is_an_error(store):
    """Something rewrote history. That is not a crash window and must not be papered over."""
    store.append("errors/en", [{"id": "e1"}, {"id": "e2"}], session="s1", batch="b1")
    path = store.stream_path("errors/en")
    lines = path.read_text().splitlines()
    path.write_text("not json\n" + "\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(CorruptStoreError):
        store.log("errors/en").read()


def test_payload_may_not_shadow_the_envelope(store):
    with pytest.raises(ValueError):
        Event(event="entry", session="s", batch="b", ordinal=0, ts="t", payload={"batch": "no"}).to_obj()


def test_reading_a_missing_stream_is_empty_not_an_error(store):
    assert EventLog(store.stream_path("errors/de")).read() == []
