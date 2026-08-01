from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from memento import MemoryStore, Proposal, Queue, StoreLock, apply_consolidation, assemble_prefix
from memento.errors import CorruptStoreError, LockTimeout, MementoError, BudgetError
from memento.events import EventLog
from conftest import base_facts, make_adapter


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "memory")
    s.initialize()
    return s


def test_bom_makes_a_preexisting_log_unreadable(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_text(
        "﻿" + json.dumps({"id": "a", "event": "entry", "ts": "t", "session": "s",
                               "batch": "b", "ordinal": 0}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(CorruptStoreError):
        EventLog(p).read()


def test_crlf_log_still_reads(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_bytes(
        (json.dumps({"id": "a", "event": "entry", "ts": "t", "session": "s",
                     "batch": "b", "ordinal": 0}) + "\r\n").encode()
    )
    assert len(EventLog(p).read()) == 1


def test_event_missing_ordinal_kills_the_stream(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_text(json.dumps({"id": "a", "event": "entry", "ts": "t", "session": "s"}) + "\n",
                 encoding="utf-8")
    with pytest.raises(CorruptStoreError):
        EventLog(p).read()


def test_non_utf8_bytes_raise_a_non_memento_error(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_bytes(b'{"id":"\xff","event":"entry","ts":"t","session":"s","batch":"b","ordinal":0}\n')
    with pytest.raises(UnicodeDecodeError):  # not a MementoError; callers cannot catch it
        EventLog(p).read()


def test_symlinked_directory_makes_recall_explode(store, tmp_path):
    outside = tmp_path / "other-store"
    (outside).mkdir()
    (outside / "x.jsonl").write_text("", encoding="utf-8")
    (store.root / "link").symlink_to(outside, target_is_directory=True)
    streams = store.streams()
    if "link/x" in streams:  # rglob followed the symlink
        with pytest.raises(MementoError):
            store.log("link/x").read()


def test_apply_consolidation_deadlocks_when_the_caller_already_holds_a_different_handle(store):
    adapter = make_adapter()
    held = StoreLock(store.locks_dir)
    with held.hold():
        with pytest.raises(LockTimeout):
            apply_consolidation(
                store, adapter, Proposal(facts=base_facts()),
                session="s", batch="b",
                lock=StoreLock(store.locks_dir, timeout=0.3),   # a *different* handle
            )


def test_prefix_overflows_the_budget_with_a_non_additive_counter(store):
    """assemble_prefix's 'truncation is exact by construction' holds only for counters where
    count(a)+count(b) >= count(a+sep+b). A merging (BPE-like) counter breaks it."""
    from memento import PrefixSection

    class MergingCounter:
        name = "merging"
        is_local = True

        def count(self, text: str) -> int:
            # a merge across the separator boundary costs an extra token
            return len(text) + (1 if "a\n\nb" in text else 0)  # one cross-boundary merge

    adapter = make_adapter(
        token_counter=MergingCounter(),
        prefix_budget_tokens=9,
        prefix_sections=(
            PrefixSection(name="a", priority=0, render=lambda s: "aaaa"),
            PrefixSection(name="b", priority=1, render=lambda s: "bbb"),
        ),
    )
    with pytest.raises(BudgetError) as exc:
        assemble_prefix(store, adapter)
    assert "assembled prefix is" in str(exc.value)  # the defensive branch, i.e. read path fails


def test_a_torn_tail_is_tolerated_on_read_then_destroyed_by_the_replay(tmp_path):
    """The documented crash path: a crash mid-append leaves a torn trailing line. read() tolerates
    it and the batch is replayed -- but append_batch appends straight onto the torn line, welding
    it to a valid event. The stream is then permanently CorruptStoreError."""
    p = tmp_path / "a.jsonl"
    good = json.dumps({"id": "a", "event": "entry", "ts": "t", "session": "s1",
                       "batch": "b1", "ordinal": 0})
    p.write_text(good + "\n" + '{"id":"b","event":"ent',  # torn tail from the crash
                 encoding="utf-8")
    log = EventLog(p)
    assert len(log.read()) == 1  # tolerated, as documented

    log.append_batch([{"id": "b"}], session="s1", batch="b2")  # the replay

    with pytest.raises(CorruptStoreError):
        log.read()  # stream is dead
