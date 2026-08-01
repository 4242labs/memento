"""Adversarial repro suite. Each test asserts the CURRENT (wrong) behaviour, so a green run here
means the defect is real. Run from the worktree root with tests/ on the path (conftest reuse)."""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path

import pytest

from memento import (
    Adapter,
    MemoryStore,
    Proposal,
    Queue,
    RetentionPolicy,
    RuleSet,
    StoreLock,
    StoreState,
    apply_consolidation,
    run_drain,
    tombstone,
)
from memento.errors import MementoError, CorruptStoreError, GateFailure
from memento.events import EventLog
from memento.gates import AntiErosionFloor, OrderedScaleFloor

from conftest import base_facts, make_adapter, StubDistiller  # noqa: E402

LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]

from memento import FrozenClock


@pytest.fixture
def clock():
    return FrozenClock()


@pytest.fixture
def store(tmp_path, clock):
    s = MemoryStore(tmp_path / "memory", clock=clock)
    s.initialize()
    return s


@pytest.fixture
def queue(tmp_path, clock):
    return Queue(tmp_path / "sessions-data", clock=clock)


@pytest.fixture
def adapter():
    return make_adapter()


# ---------------------------------------------------------------- F1  jubs layout


def test_F1_append_corrupts_preexisting_log_without_trailing_newline(tmp_path):
    """A jubs-era JSONL log whose last line has no trailing \\n is silently corrupted by the first
    append, and is then unreadable FOREVER (CorruptStoreError on a mid-file line)."""
    p = tmp_path / "vocab" / "en.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text(
        '{"id":"a","event":"entry","ts":"t","session":"old","batch":"ob","ordinal":0}',  # no \n
        encoding="utf-8",
    )
    log = EventLog(p)
    assert len(log.read()) == 1  # reads fine today

    log.append_batch([{"id": "b"}], session="s2", batch="b2")

    raw = p.read_text(encoding="utf-8")
    assert '}{"' in raw  # <-- two events welded into one line
    with pytest.raises(CorruptStoreError):
        log.read()  # the whole stream is now permanently unreadable


# ---------------------------------------------------------------- F2  queue path escape


def test_F2_queue_session_id_escapes_the_queue_root(tmp_path):
    q = Queue(tmp_path / "q" / "inner")
    q.close_and_enqueue("../../../escaped")
    q.append_turn("../../../escaped", 1, {"text": "hi"})
    q.mark_consolidated("../../../escaped")
    assert (tmp_path.parent / "escaped" / "journal.jsonl").exists()
    assert (tmp_path.parent / "escaped" / "consolidated").exists()

    # absolute session id ignores the root entirely
    target = tmp_path / "absolutely-elsewhere"
    q.append_turn(str(target), 1, {"text": "hi"})
    assert (target / "journal.jsonl").exists()


def test_F2b_prune_deletes_a_file_outside_the_queue_root(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "journal.jsonl").write_text("precious\n", encoding="utf-8")

    q = Queue(
        tmp_path / "q",
        retention=RetentionPolicy(keep_everything=False, prune_after_consolidation=True),
    )
    q.mark_consolidated("../victim")  # marker written outside the root, then prune fires
    assert not (victim / "journal.jsonl").exists()  # arbitrary file deleted


# ------------------------------------------------------- F3  floor off for anonymous lists


def _floor_ok(current_facts, proposed_facts, tombstones=(), scales=None):
    rs = RuleSet((), ordered_scales=scales or {})
    return rs.check(
        StoreState(facts=current_facts, tombstones=set(tombstones)),
        Proposal(facts=proposed_facts),
    )


def test_F3_identity_less_list_members_are_positional_so_substitution_is_invisible():
    """The adapter names its identity field `lang` (not id/topic/name). Members are then addressed
    by POSITION, so replacing one language with another erodes the store with the floor green."""
    current = {"languages": [{"lang": "fr", "level": "B1"}, {"lang": "de", "level": "A1"}]}
    proposed = {"languages": [{"lang": "fr", "level": "B1"}, {"lang": "it", "level": "A1"}]}
    assert _floor_ok(current, proposed) == []  # `de` is gone; no violation
    # same for a list of records keyed by any other field name
    cur2 = {"interests": [{"label": "kites"}, {"label": "lighthouses"}]}
    assert _floor_ok(cur2, {"interests": [{"label": "kites"}, {"label": "crochet"}]}) == []


def test_F3d_duplicate_ids_collapse_so_one_of_them_can_be_deleted_invisibly():
    current = {"notes": [{"id": "a", "text": "first"}, {"id": "a", "text": "second"}]}
    assert _floor_ok(current, {"notes": [{"id": "a", "text": "first"}]}) == []
    # int/str id collision, same effect
    current2 = {"notes": [{"id": 1, "text": "x"}, {"id": "1", "text": "y"}]}
    assert _floor_ok(current2, {"notes": [{"id": 1, "text": "x"}]}) == []


def test_F3e_dict_to_list_type_change_destroys_every_value():
    """Keys survive, values are annihilated, floor sees nothing."""
    current = {"prefs": {"tone": "blunt", "language": "pt", "pace": "fast"}}
    assert _floor_ok(current, {"prefs": ["tone", "language", "pace"]}) == []


# ------------------------------------------------- F4  ordered scale skip -> multi-step jump


def test_F4_ordered_scale_multi_step_jump_via_positional_key_drift():
    """Anonymous list + an inserted member shifts positions; _get(current, 'langs.1.level') misses,
    OrderedScaleFloor skips, and `es` jumps A1 -> C2 (5 steps) with the floor fully green."""
    current = {"langs": [{"lang": "es", "level": "A1"}]}
    proposed = {"langs": [{"lang": "de", "level": "A1"}, {"lang": "es", "level": "C2"}]}
    assert _floor_ok(current, proposed, scales={"langs.*.level": LEVELS}) == []


# --------------------------------------------- F5  one tombstone authorizes unrelated deletes


def test_F5_a_bare_key_tombstone_authorizes_that_key_anywhere_in_the_tree():
    """`key in allowed` ignores the path entirely: one tombstone clears every same-named member."""
    current = {
        "de": "a top-level fact the operator asked to forget",
        "notifications": {"de": "on", "fr": "on"},
        "tags": ["de", "fr"],
        "contacts": {"de": "Dieter"},
    }
    proposed = {"notifications": {"fr": "on"}, "tags": ["fr"], "contacts": {}}
    assert _floor_ok(current, proposed, tombstones={"de"}) == []  # four deletions, one tombstone


def test_F5b_end_to_end_forgetting_a_top_level_fact_authorizes_unrelated_deletions(store, adapter):
    from memento.forgetting import forget_fact

    facts = base_facts()
    facts["de"] = "operator once lived in Germany"          # a top-level fact
    facts["notifications"] = {"de": "on", "fr": "on"}       # unrelated subtree
    facts["contacts"] = {"de": "Dieter", "fr": "Amelie"}    # another one
    apply_consolidation(store, adapter, Proposal(facts=facts), session="s1", batch="b1")

    forget_fact(store, adapter, "de", session="op", batch="f1")  # marker is the bare key "de"

    eroded = json.loads(store.read_document(".memento/facts.json"))
    del eroded["notifications"]["de"]  # <- never authorised by any tombstone
    del eroded["contacts"]["de"]       # <- nor this
    apply_consolidation(store, adapter, Proposal(facts=eroded), session="s2", batch="b2")

    stored = json.loads(store.read_document(".memento/facts.json"))
    assert "de" not in stored["notifications"] and "de" not in stored["contacts"]


# ------------------------------------------------------------ F6  a dot in a key bricks writes


def test_F6_a_key_containing_a_dot_makes_every_consolidation_fail_forever():
    """Identical facts in and out, yet the floor reports 'collection disappeared'."""
    facts = {"interests": [{"topic": "node.js", "engagement": "high"}]}
    violations = _floor_ok(facts, copy.deepcopy(facts))
    assert violations, "expected the no-op proposal to be rejected"
    assert "disappeared" in violations[0].detail

    facts2 = {"languages": {"pt.br": {"level": "B1"}}}
    assert _floor_ok(facts2, copy.deepcopy(facts2))


def test_F6b_end_to_end_a_dotted_topic_locks_the_store(store, adapter):
    facts = base_facts()
    facts["interests"].append({"topic": "node.js", "engagement": "low", "notes": ""})
    apply_consolidation(store, adapter, Proposal(facts=facts), session="s1", batch="b1")
    with pytest.raises(GateFailure):  # re-proposing the SAME facts is now impossible
        apply_consolidation(
            store, adapter, Proposal(facts=copy.deepcopy(facts)), session="s2", batch="b2"
        )


# ------------------------------------------------------------ F7  secrets gate bypass


def test_F7_replace_document_and_cli_edit_bypass_the_secrets_gate(store, tmp_path, capsys):
    from memento.cli import main

    secret = "sk-ant-" + "A" * 40
    store.replace_document("profile.md", f"key: {secret}\n", session="s", batch="b")
    assert secret in store.read_document("profile.md")

    src = tmp_path / "paste.md"
    src.write_text(f"AKIAIOSFODNN7EXAMPLE and {secret}\n", encoding="utf-8")
    rc = main(["--store", str(store.root), "edit", "notes.md", "--from-file", str(src)])
    assert rc == 0
    assert "AKIAIOSFODNN7EXAMPLE" in store.read_document("notes.md")


# ------------------------------------------------------------ F8  StoreLock across threads


def test_F8_storelock_depth_is_not_thread_safe_and_corrupts_permanently(tmp_path):
    lock = StoreLock(tmp_path)
    inside = []
    both_in = threading.Event()

    def worker_a():
        with lock.hold():
            inside.append("a")
            both_in.wait(2)

    def worker_b():
        time.sleep(0.05)
        with lock.hold():  # should block; takes the re-entrant path instead
            inside.append("b")
            both_in.set()
            time.sleep(0.1)

    ta, tb = threading.Thread(target=worker_a), threading.Thread(target=worker_b)
    ta.start(), tb.start()
    ta.join(3), tb.join(3)

    assert inside == ["a", "b"]  # both threads were inside the critical section at once
    assert lock._depth == -1  # counter underflowed
    # and now the lock is a permanent no-op: -1 is truthy, so flock is never taken again
    with lock.hold():
        assert lock._fd is None


# ------------------------------------- F9  redrive after a crash wedges the drain forever


def _crashing_write(monkeypatch, fail_on: int):
    calls = {"n": 0}
    real = None
    import memento.store as store_mod

    real = store_mod._atomic_write_text

    def fake(path, content):
        calls["n"] += 1
        if calls["n"] == fail_on:
            raise RuntimeError("power cut between the event and the file")
        return real(path, content)

    monkeypatch.setattr(store_mod, "_atomic_write_text", fake)
    return calls


def test_F9_crash_in_the_replace_window_then_a_different_proposal_is_unrecoverable(
    store, adapter, monkeypatch
):
    p1 = Proposal(facts=base_facts())
    _crashing_write(monkeypatch, fail_on=1)
    with pytest.raises(RuntimeError):
        apply_consolidation(store, adapter, p1, session="s1", batch="drain-s1")
    monkeypatch.undo()

    # the document_replaced events landed; the files did not
    assert store.document_log().has_batch("s1", "drain-s1")
    assert store.read_document("profile.md") is None

    # the redrive re-runs the LLM, which produces slightly different output (the normal case)
    facts2 = base_facts()
    facts2["languages"]["fr"]["goals"] = "read philosophy unaided, slowly"
    with pytest.raises(MementoError):
        apply_consolidation(store, adapter, Proposal(facts=facts2), session="s1", batch="drain-s1")
    # ...and it will raise on every future attempt: the session can never be consolidated.


def test_F9b_that_exception_kills_the_whole_drain_and_strands_later_sessions(
    store, adapter, queue, monkeypatch
):
    _crashing_write(monkeypatch, fail_on=1)
    with pytest.raises(RuntimeError):
        apply_consolidation(store, adapter, Proposal(facts=base_facts()), session="s1",
                            batch="drain-s1")
    monkeypatch.undo()

    queue.close_and_enqueue("s1")
    queue.close_and_enqueue("s2")
    facts2 = base_facts()
    facts2["languages"]["fr"]["goals"] = "changed"
    distiller = StubDistiller(proposal=Proposal(facts=facts2))

    with pytest.raises(MementoError):  # run_drain does not catch this
        run_drain(store, adapter, distiller, queue, do_push=False)

    assert not queue.is_consolidated("s1")
    assert not queue.is_consolidated("s2")  # s2 never even reached


# ------------------------------- F10  same (session,batch), different entries: silent drop


def test_F10_replayed_batch_drops_new_entries_but_reports_them_written(store, adapter):
    facts = base_facts()
    r1 = apply_consolidation(
        store,
        adapter,
        Proposal(facts=facts, entries={"errors/fr": [{"id": "fr-un-erreur", "pattern": "un erreur"}]}),
        session="s1",
        batch="b1",
    )
    assert r1.ok
    r2 = apply_consolidation(
        store,
        adapter,
        Proposal(
            facts=copy.deepcopy(facts),
            entries={"errors/fr": [{"id": "fr-deux-erreur", "pattern": "deux erreur"}]},
        ),
        session="s1",
        batch="b1",
    )
    assert r2.ok is True
    assert r2.streams_written == ["errors/fr"]  # claims it wrote
    ids = set(store.folded("errors/fr"))
    assert ids == {"fr-un-erreur"}  # the second entry was silently discarded


# --------------------------------------------- F11  lost write between two concurrent drains


def test_F11_two_drains_lose_a_write_last_writer_wins(store, adapter, tmp_path, clock):
    apply_consolidation(store, adapter, Proposal(facts=base_facts()), session="s0", batch="b0")

    # both front-ends read the same state before either writes (no lock is held across the call)
    state_a = json.loads(store.read_document(".memento/facts.json"))
    state_b = copy.deepcopy(state_a)

    state_a["languages"]["fr"]["goals"] = "A's freshly learned goal"
    apply_consolidation(store, adapter, Proposal(facts=state_a), session="sA", batch="bA")

    state_b["languages"]["de"]["goals"] = "B's freshly learned goal"
    apply_consolidation(store, adapter, Proposal(facts=state_b), session="sB", batch="bB")

    final = json.loads(store.read_document(".memento/facts.json"))
    assert final["languages"]["de"]["goals"] == "B's freshly learned goal"
    assert final["languages"]["fr"]["goals"] != "A's freshly learned goal"  # A's write is gone
