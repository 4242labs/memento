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


def test_repair_tail_is_public_so_a_consumer_appender_cannot_weld(store):
    """A consumer with its own idempotency rule still appends through the same file.

    jubs keys its batches on `(id, event, ordinal)` where the engine keys on
    `(session, batch)`, so it filters the batch itself and writes the missing tail. Doing that
    onto a torn final line welds the fragment onto the next event — and the result is a bad
    line in the *middle* of the log, which `read` refuses to skip, so the whole stream is lost
    from that point on. The repair the engine already runs before its own appends has to be
    reachable, or every consumer reimplements it and gets it wrong.
    """
    store.append("errors/en", [{"id": "e1"}], session="s1", batch="b1")
    path = store.stream_path("errors/en")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"id": "e2", "event": "entry", "ts": "2026-')  # power cut, mid-line

    log = EventLog(path)
    log.repair_tail()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "e3", "event": "entry", "ts": "t", "session": "s1",
                             "batch": "b2", "ordinal": 0}) + "\n")

    assert [e.id for e in log.read()] == ["e1", "e3"]


def test_appending_onto_a_torn_tail_without_repairing_it_destroys_the_stream(store):
    """The failure the public repair exists to prevent — asserted, so it cannot be argued away."""
    store.append("errors/en", [{"id": "e1"}], session="s1", batch="b1")
    path = store.stream_path("errors/en")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"id": "e2", "event": "entry", "ts": "2026-')
    with open(path, "a", encoding="utf-8") as fh:  # no repair: the weld
        fh.write(json.dumps({"id": "e3", "event": "entry", "ts": "t", "session": "s1",
                             "batch": "b2", "ordinal": 0}) + "\n")

    with pytest.raises(CorruptStoreError):
        EventLog(path).read()
