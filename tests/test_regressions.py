"""Regressions from the adversarial review of the first build.

Every test here corresponds to a defect that shipped in the first implementation and passed a
172-test suite written by the same author. They are collected in one file on purpose: this is the
list of things that were confidently believed and were wrong, and it is worth reading as such.

Each one asserts the *corrected* behaviour.
"""

from __future__ import annotations

import copy
import json
import os
import threading

import pytest

from conftest import StubDistiller, base_facts, make_adapter
from memento import (
    Adapter,
    LockTimeout,
    CorruptStoreError,
    GateFailure,
    MementoError,
    PrefixSection,
    Proposal,
    Queue,
    RetentionPolicy,
    RuleSet,
    SecretsDetected,
    StaleProposal,
    StoreLock,
    StoreState,
    apply_consolidation,
    assemble_prefix,
    run_drain,
)
from memento.events import EventLog
from memento.forgetting import document_revisions, rollback_document
from memento.writepath import UNCHECKED, facts_fingerprint, read_facts

LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]


def floor_violations(current, proposed, tombstones=(), scales=None, identity_keys=None):
    rules = RuleSet(
        (),
        ordered_scales=scales or {},
        **({"identity_keys": identity_keys} if identity_keys else {}),
    )
    return rules.check(
        StoreState(facts=current, tombstones=set(tombstones)), Proposal(facts=proposed)
    )


# ============================================================ C1 — torn tails


def test_appending_after_a_torn_tail_does_not_weld_two_events_together(tmp_path):
    """The documented crash path used to destroy the stream on the very next append."""
    path = tmp_path / "a.jsonl"
    good = json.dumps(
        {"id": "a", "event": "entry", "ts": "t", "session": "s1", "batch": "b1", "ordinal": 0}
    )
    path.write_text(good + "\n" + '{"id":"b","event":"ent', encoding="utf-8")
    log = EventLog(path)
    assert len(log.read()) == 1  # torn tail tolerated

    log.append_batch([{"id": "b", "pattern": "replayed"}], session="s1", batch="b2")

    events = log.read()  # and still readable afterwards, which is the whole point
    assert [e.id for e in events] == ["a", "b"]
    assert '}{"' not in path.read_text(encoding="utf-8")


def test_a_pre_existing_log_without_a_trailing_newline_survives_an_append(tmp_path):
    """A complete final event with no newline is a pre-engine log, not a torn one. Keep it."""
    path = tmp_path / "vocab" / "fr.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"id":"a","event":"entry","ts":"t","session":"old","batch":"ob","ordinal":0}',
        encoding="utf-8",
    )
    log = EventLog(path)
    log.append_batch([{"id": "b"}], session="s2", batch="b2")

    assert [e.id for e in log.read()] == ["a", "b"]


# ====================================================== C2 — redrive after a crash


def _crash_the_next_file_write(monkeypatch):
    import memento.store as store_mod

    real = store_mod._atomic_write_text
    state = {"armed": True}

    def fake(path, content):
        if state["armed"]:
            state["armed"] = False
            raise RuntimeError("power cut between the event and the file")
        return real(path, content)

    monkeypatch.setattr(store_mod, "_atomic_write_text", fake)


def test_a_redrive_with_different_output_still_lands(store, adapter, monkeypatch):
    """The crash window's whole point. The model re-runs and does not repeat itself byte for byte."""
    _crash_the_next_file_write(monkeypatch)
    with pytest.raises(RuntimeError):
        apply_consolidation(store, adapter, Proposal(facts=base_facts()), session="s1", batch="b1", expected_fingerprint=UNCHECKED)
    monkeypatch.undo()

    second = base_facts()
    second["languages"]["fr"]["goals"] = "read philosophy unaided, slowly"
    result = apply_consolidation(store, adapter, Proposal(facts=second), session="s1", batch="b1", expected_fingerprint=UNCHECKED)

    assert result.ok
    assert read_facts(store)["languages"]["fr"]["goals"] == "read philosophy unaided, slowly"


def test_one_failing_session_does_not_strand_the_rest_of_the_drain(store, adapter, queue, clock):
    """Bounded, visible, never fatal — including when the failure is not a gate rejection."""
    queue.close_and_enqueue("s1")
    queue.close_and_enqueue("s2")

    calls = {"n": 0}

    def explode_once(journal, state):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ZeroDivisionError("something no one anticipated")

    distiller = StubDistiller(proposal=Proposal(facts=base_facts()), on_call=explode_once)
    report = run_drain(store, adapter, distiller, queue, clock=clock, do_push=False)

    assert report.consolidated == ["s2"]  # the second session was still reached
    assert [s for s, _ in report.deferred] == ["s1"]
    assert not queue.is_consolidated("s1") and queue.is_consolidated("s2")


# ================================================ C3 / M4 / M8 — the floor fails closed


def test_a_list_whose_members_carry_no_recognised_identity_is_a_violation():
    """It used to fall back to positional matching, which is the failure it exists to prevent."""
    current = {"languages": [{"lang": "fr", "level": "B1"}, {"lang": "de", "level": "A1"}]}
    proposed = {"languages": [{"lang": "fr", "level": "B1"}, {"lang": "it", "level": "A1"}]}

    violations = floor_violations(current, proposed)

    assert violations and "no identity" in violations[0].detail


def test_an_adapter_may_declare_its_own_identity_key_and_then_the_floor_works(store):
    """Fail-closed is not a dead end: name the field and the check comes back on."""
    adapter = Adapter(name="by-lang", identity_keys=("lang", "id", "topic", "name"))
    current = {"languages": [{"lang": "fr", "level": "B1"}, {"lang": "de", "level": "A1"}]}
    apply_consolidation(store, adapter, Proposal(facts=current), session="s1", batch="b1", expected_fingerprint=UNCHECKED)

    substituted = {"languages": [{"lang": "fr", "level": "B1"}, {"lang": "it", "level": "A1"}]}
    with pytest.raises(GateFailure, match="languages/de"):
        apply_consolidation(store, adapter, Proposal(facts=substituted), session="s2", batch="b2", expected_fingerprint=UNCHECKED)


def test_two_members_with_the_same_identity_cannot_be_verified():
    current = {"notes": [{"id": "a", "text": "first"}, {"id": "a", "text": "second"}]}
    violations = floor_violations(current, {"notes": [{"id": "a", "text": "first"}]})
    assert violations and "same identity" in violations[0].detail


def test_an_int_and_a_string_identity_do_not_collide_silently():
    current = {"notes": [{"id": 1, "text": "x"}, {"id": "1", "text": "y"}]}
    assert floor_violations(current, {"notes": [{"id": 1, "text": "x"}]})


def test_turning_a_mapping_into_a_list_of_its_keys_is_a_violation():
    """Every member key survives; every value is destroyed. The floor used to see nothing."""
    current = {"prefs": {"tone": "blunt", "language": "pt", "pace": "fast"}}
    violations = floor_violations(current, {"prefs": ["tone", "language", "pace"]})
    assert violations and "changed from mapping to sequence" in violations[0].detail


def test_an_ordered_scale_cannot_jump_by_drifting_positions():
    """Inserting a member used to shift positions, miss the old value, and skip the check."""
    current = {"langs": [{"lang": "es", "level": "A1"}]}
    proposed = {"langs": [{"lang": "de", "level": "A1"}, {"lang": "es", "level": "C2"}]}

    violations = floor_violations(
        current,
        proposed,
        scales={"langs.*.level": LEVELS},
        identity_keys=("lang", "id", "topic", "name"),
    )

    assert any("5 steps" in v.detail for v in violations)


# ================================================== M5 — tombstone scope


def test_a_tombstone_authorizes_exactly_one_retirement():
    """`de` at the root used to authorize dropping `de` anywhere else in the tree."""
    current = {
        "de": "a top-level fact the operator asked to forget",
        "notifications": {"de": "on", "fr": "on"},
        "contacts": {"de": "Dieter"},
    }
    proposed = {"notifications": {"fr": "on"}, "contacts": {}}

    violations = floor_violations(current, proposed, tombstones={"de"})

    dropped = {v.path for v in violations}
    assert "notifications/de" in dropped
    assert "contacts/de" in dropped


def test_a_tombstone_still_covers_what_is_inside_the_thing_it_retires(store, adapter):
    shrunk = base_facts()
    del shrunk["languages"]["de"]
    apply_consolidation(store, adapter, Proposal(facts=base_facts()), session="s1", batch="b1", expected_fingerprint=UNCHECKED)

    result = apply_consolidation(
        store,
        adapter,
        Proposal(facts=shrunk, tombstones={"languages/de"}),
        session="s2",
        batch="b2",
        expected_fingerprint=UNCHECKED,
    )
    assert result.ok  # one marker, and the fields inside `de` go with it


# ================================================== M6 — dots in keys


@pytest.mark.parametrize(
    "facts",
    [
        {"interests": [{"topic": "node.js", "engagement": "high"}]},
        {"languages": {"pt.br": {"level": "B1"}}},
        {"papers": [{"id": "arXiv:2604.06710", "read": "yes"}]},
    ],
)
def test_a_key_containing_a_dot_is_ordinary_data(facts):
    """Dotted path strings mis-split on it, so identical facts read as a deletion and the
    all-or-nothing write meant no consolidation could ever land again."""
    assert floor_violations(facts, copy.deepcopy(facts)) == []


def test_a_dotted_topic_can_be_stored_and_then_re_proposed(store, adapter):
    facts = base_facts()
    facts["interests"].append({"topic": "node.js", "engagement": "low", "notes": ""})
    apply_consolidation(store, adapter, Proposal(facts=facts), session="s1", batch="b1", expected_fingerprint=UNCHECKED)

    assert apply_consolidation(
        store, adapter, Proposal(facts=copy.deepcopy(facts)), session="s2", batch="b2",
        expected_fingerprint=UNCHECKED,
    ).ok


def test_a_dotted_member_can_still_be_forgotten(store, adapter):
    from memento.forgetting import forget_fact

    facts = base_facts()
    facts["interests"].append({"topic": "node.js", "engagement": "low", "notes": ""})
    apply_consolidation(store, adapter, Proposal(facts=facts), session="s1", batch="b1", expected_fingerprint=UNCHECKED)

    forget_fact(store, adapter, "interests/node.js", session="op", batch="f1")

    assert [i["topic"] for i in read_facts(store)["interests"]] == ["kite building", "lighthouses"]


# ================================================== M1 — queue path escapes


@pytest.mark.parametrize("bad", ["../../../escaped", "/absolutely/elsewhere", "..", "a/b", ""])
def test_the_queue_refuses_a_session_id_that_is_not_a_path_segment(tmp_path, bad):
    queue = Queue(tmp_path / "q")
    with pytest.raises(MementoError, match="invalid session id"):
        queue.close_and_enqueue(bad)


def test_pruning_cannot_reach_outside_the_queue_root(tmp_path):
    """With pruning on, an unchecked id meant arbitrary file deletion."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "journal.jsonl").write_text("precious\n", encoding="utf-8")

    queue = Queue(
        tmp_path / "q",
        retention=RetentionPolicy(keep_everything=False, prune_after_consolidation=True),
    )
    with pytest.raises(MementoError):
        queue.mark_consolidated("../victim")

    assert (victim / "journal.jsonl").exists()


def test_a_claim_refuses_the_same_ids(store):
    from memento import SessionClaim

    with pytest.raises(MementoError, match="invalid session id"):
        SessionClaim(store.locks_dir, "../../elsewhere")


# ================================================== M2 — the secrets gate is a gate


def test_replace_document_is_gated(store):
    with pytest.raises(SecretsDetected):
        store.replace_document(
            "profile.md", "key: sk-ant-" + "A" * 40 + "\n", session="s", batch="b"
        )
    assert store.read_document("profile.md") is None


def test_the_cli_edit_verb_is_gated(store, tmp_path, capsys):
    from memento.cli import main

    paste = tmp_path / "paste.md"
    paste.write_text("AKIA" + "IOSFODNN7EXAMPLE" + "\n", encoding="utf-8")

    with pytest.raises(SecretsDetected):
        main(["--store", str(store.root), "edit", "notes.md", "--from-file", str(paste)])

    assert store.read_document("notes.md") is None


def test_a_direct_event_append_is_gated(store):
    with pytest.raises(SecretsDetected):
        store.append(
            "vocab/fr", [{"id": "v1", "item": "AKIA" + "IOSFODNN7EXAMPLE"}], session="s", batch="b"
        )
    assert store.log("vocab/fr").read() == []


def test_a_session_log_is_gated(store):
    with pytest.raises(SecretsDetected):
        store.write_session_log("s1", "they pasted ghp_" + "a" * 36)
    assert store.session_logs() == []


# ================================================== M3 — StoreLock across threads


def test_a_second_thread_cannot_enter_while_the_first_holds(store):
    """Forced interleaving, on purpose.

    An unsynchronised race passes against the *broken* lock too — four threads all clear the depth
    check before the first one sets it — so the earlier version of this test proved nothing. Here
    the contender does not start until the holder is provably inside.
    """
    lock = StoreLock(store.locks_dir, timeout=0.3)
    holder_inside = threading.Event()
    outcome: list[str] = []

    def holder():
        with lock.hold():
            holder_inside.set()
            threading.Event().wait(0.4)

    def contender():
        assert holder_inside.wait(timeout=5)
        try:
            with lock.hold(timeout=0.15):
                outcome.append("entered")  # the broken lock lands here
        except LockTimeout:
            outcome.append("blocked")

    threads = [threading.Thread(target=holder), threading.Thread(target=contender)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert outcome == ["blocked"]
    assert not lock.held  # and the state did not underflow on the way out


def test_a_forked_child_does_not_inherit_the_lock(store):
    """A child forked mid-hold used to skip the flock entirely and write inside the parent's
    critical section, because it inherited a depth of 1."""
    lock = StoreLock(store.locks_dir, timeout=0.3)
    read_fd, write_fd = os.pipe()

    with lock.hold():
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child process
            os._exit(0 if not StoreLock(store.locks_dir, timeout=0.2).held else 3)
        os.close(write_fd)
        _, status = os.waitpid(pid, 0)

    os.close(read_fd)
    assert os.WEXITSTATUS(status) == 0


# ================================================== M7 — a replayed batch is not a silent drop


def test_a_redrive_with_different_entries_keeps_both_rather_than_wedging(store, adapter):
    """Three behaviours have stood here, and only the third is right.

    Dropping the new entries and reporting success lost half a consolidation. Raising instead
    wedged the session forever, because a drain keys its batch on the session id and every redrive
    hit the same error — a worse failure than the loss it replaced. Both land now.
    """
    facts = base_facts()
    apply_consolidation(
        store,
        adapter,
        Proposal(facts=facts, entries={"errors/fr": [{"id": "fr-un-erreur", "pattern": "un erreur"}]}),
        session="s1",
        batch="b1",
        expected_fingerprint=UNCHECKED,
    )

    apply_consolidation(
        store,
        adapter,
        Proposal(
            facts=copy.deepcopy(facts),
            entries={"errors/fr": [{"id": "fr-deux-erreur", "pattern": "deux erreur"}]},
        ),
        session="s1",
        batch="b1",
        expected_fingerprint=UNCHECKED,
    )

    assert set(store.folded("errors/fr")) == {"fr-un-erreur", "fr-deux-erreur"}


def test_replaying_the_identical_batch_is_still_a_no_op(store, adapter):
    facts = base_facts()
    proposal = Proposal(
        facts=facts, entries={"errors/fr": [{"id": "fr-un-erreur", "pattern": "un erreur"}]}
    )
    apply_consolidation(store, adapter, proposal, session="s1", batch="b1", expected_fingerprint=UNCHECKED)
    apply_consolidation(store, adapter, copy.deepcopy(proposal), session="s1", batch="b1", expected_fingerprint=UNCHECKED)

    assert len(store.log("errors/fr").read()) == 1


# ================================================== M9 — no lost write


def test_a_proposal_derived_from_stale_facts_is_refused(store, adapter, queue):
    """Two drains read the same state before either wrote; the loser must redrive, not overwrite."""
    apply_consolidation(store, adapter, Proposal(facts=base_facts()), session="s0", batch="b0", expected_fingerprint=UNCHECKED)
    snapshot = read_facts(store)
    fingerprint = facts_fingerprint(snapshot)

    landed = copy.deepcopy(snapshot)
    landed["languages"]["fr"]["goals"] = "A's freshly learned goal"
    apply_consolidation(store, adapter, Proposal(facts=landed), session="sA", batch="bA", expected_fingerprint=UNCHECKED)

    stale = copy.deepcopy(snapshot)
    stale["languages"]["de"]["goals"] = "B's freshly learned goal"
    queue.close_and_enqueue("sB")

    with pytest.raises(StaleProposal):
        apply_consolidation(
            store,
            adapter,
            Proposal(facts=stale),
            session="sB",
            batch="bB",
            queue=queue,
            expected_fingerprint=fingerprint,
        )

    assert read_facts(store)["languages"]["fr"]["goals"] == "A's freshly learned goal"
    assert queue.pending_sessions()[0].deferrals == 1  # sB stays pending for a redrive


def test_a_drain_supplies_the_fingerprint_itself(store, adapter, queue, clock):
    """So the protection does not depend on a consumer remembering to ask for it."""
    queue.close_and_enqueue("s1")

    def land_something_else(journal, state):
        other = base_facts()
        other["languages"]["fr"]["goals"] = "written by the other front-end"
        apply_consolidation(store, adapter, Proposal(facts=other), session="other", batch="b", expected_fingerprint=UNCHECKED)

    distiller = StubDistiller(proposal=Proposal(facts=base_facts()), on_call=land_something_else)
    report = run_drain(store, adapter, distiller, queue, clock=clock, do_push=False)

    assert report.consolidated == []
    assert any(f.kind == "stale-proposal" for f in report.flags)
    assert read_facts(store)["languages"]["fr"]["goals"] == "written by the other front-end"


# ================================================== m2 — hostile pre-existing logs


def _one_event() -> dict:
    return {"id": "a", "event": "entry", "ts": "t", "session": "s", "batch": "b", "ordinal": 0}


def test_a_byte_order_mark_does_not_make_a_log_unreadable(tmp_path):
    path = tmp_path / "a.jsonl"
    path.write_text("﻿" + json.dumps(_one_event()) + "\n", encoding="utf-8")
    assert [e.id for e in EventLog(path).read()] == ["a"]


def test_crlf_line_endings_read(tmp_path):
    path = tmp_path / "a.jsonl"
    path.write_bytes((json.dumps(_one_event()) + "\r\n").encode())
    assert len(EventLog(path).read()) == 1


def test_an_event_missing_envelope_fields_still_reads(tmp_path):
    """A pre-engine log is the compatibility target; refusing it would make adoption a migration."""
    path = tmp_path / "a.jsonl"
    path.write_text(
        json.dumps({"id": "a", "event": "entry", "ts": "t", "session": "s"}) + "\n",
        encoding="utf-8",
    )
    (event,) = EventLog(path).read()
    assert event.id == "a" and event.ordinal == 0


def test_non_utf8_bytes_raise_something_callers_can_catch(tmp_path):
    path = tmp_path / "a.jsonl"
    path.write_bytes(b'{"id":"\xff","event":"entry","ts":"t","session":"s","batch":"b","ordinal":0}\n')
    with pytest.raises(CorruptStoreError):
        EventLog(path).read()


def test_a_symlink_out_of_the_store_is_not_listed_as_a_stream(store, tmp_path):
    """A symlinked *file*, deliberately.

    The first version of this test linked a directory, which `rglob` does not descend into on any
    supported Python — so it passed before the filter existed and proved nothing. A symlinked file
    is yielded, and is the case the filter actually has to catch.
    """
    outside = tmp_path / "other-store"
    outside.mkdir()
    (outside / "x.jsonl").write_text('{"id":"a","event":"entry"}\n', encoding="utf-8")
    (store.root / "borrowed.jsonl").symlink_to(outside / "x.jsonl")

    assert (store.root / "borrowed.jsonl").exists()  # the bait is really there
    assert "borrowed" not in store.streams()


# ================================================== m3 — a non-additive counter


def test_the_budget_holds_for_a_counter_that_merges_across_the_separator(store):
    """Per-section arithmetic is a prediction; the budget is a promise about the assembled whole."""

    class MergingCounter:
        name = "merging"
        is_local = True

        def count(self, text: str) -> int:
            return len(text) + (1 if "a\n\nb" in text else 0)

    adapter = make_adapter(
        token_counter=MergingCounter(),
        prefix_budget_tokens=9,
        prefix_sections=(
            PrefixSection(name="a", priority=0, render=lambda s: "aaaa"),
            PrefixSection(name="b", priority=1, render=lambda s: "bbb"),
        ),
    )

    result = assemble_prefix(store, adapter)

    assert MergingCounter().count(result.text) <= 9
    assert result.tokens <= 9


# ================================================== m4 — a pre-existing .gitignore


def test_enabling_backup_extends_an_existing_gitignore(store):
    from memento import enable_backup

    (store.root / ".gitignore").write_text("# the store owner's own rules\n*.tmp\n", encoding="utf-8")
    enable_backup(store, acknowledged=True)

    text = (store.root / ".gitignore").read_text()
    assert "*.tmp" in text  # theirs is kept
    assert ".memento/locks/" in text and ".memento/queue/" in text  # and ours is added


# ================================================== m1 — no self-deadlock


def test_an_operator_forget_inside_a_held_lock_does_not_deadlock(store, adapter):
    from memento.forgetting import forget_fact

    apply_consolidation(store, adapter, Proposal(facts=base_facts()), session="s1", batch="b1", expected_fingerprint=UNCHECKED)

    with StoreLock(store.locks_dir, timeout=1.0).hold():
        forget_fact(store, adapter, "languages/de", session="op", batch="f1")

    assert "de" not in read_facts(store)["languages"]


def test_apply_consolidation_refuses_to_guess_a_baseline(store, adapter):
    """The residual from the first round of fixes: silence used to mean "check nothing".

    Defaulting the fingerprint hid the one decision only the caller can make, and the caller who
    forgets is exactly the caller whose write gets lost.
    """
    with pytest.raises(MementoError, match="requires expected_fingerprint"):
        apply_consolidation(store, adapter, Proposal(facts=base_facts()), session="s1", batch="b1")


def test_two_writers_from_one_snapshot_cannot_both_land(store, adapter):
    """The bare-API form of the lost write, now closed for every caller rather than just drains."""
    apply_consolidation(
        store,
        adapter,
        Proposal(facts=base_facts()),
        session="s0",
        batch="b0",
        expected_fingerprint=UNCHECKED,
    )
    snapshot = read_facts(store)
    fingerprint = facts_fingerprint(snapshot)

    first = copy.deepcopy(snapshot)
    first["languages"]["fr"]["goals"] = "A's freshly learned goal"
    apply_consolidation(
        store, adapter, Proposal(facts=first), session="sA", batch="bA",
        expected_fingerprint=fingerprint,
    )

    second = copy.deepcopy(snapshot)
    second["languages"]["de"]["goals"] = "B's freshly learned goal"
    with pytest.raises(StaleProposal):
        apply_consolidation(
            store, adapter, Proposal(facts=second), session="sB", batch="bB",
            expected_fingerprint=fingerprint,
        )

    assert read_facts(store)["languages"]["fr"]["goals"] == "A's freshly learned goal"


# ============================================ round two — regressions in the fixes


def test_a_drain_redrive_with_entries_is_not_a_dead_end(store, adapter, queue, clock):
    """The worst regression the fixes introduced.

    A drain keys its batch on the session id, so every redrive of a session that had entries hit
    the same "already recorded with different entries" error and deferred again. The session could
    never consolidate — a permanent wedge in place of the silent drop it replaced.
    """
    queue.close_and_enqueue("s1")

    first = Proposal(
        facts=base_facts(), entries={"errors/fr": [{"id": "fr-un-erreur", "pattern": "un erreur"}]}
    )
    import memento.store as store_mod

    real = store_mod._atomic_write_text
    armed = {"yes": True}

    def crash_once(path, content):
        if armed["yes"]:
            armed["yes"] = False
            raise RuntimeError("power cut")
        return real(path, content)

    store_mod._atomic_write_text = crash_once
    try:
        run_drain(store, adapter, StubDistiller(proposal=first), queue, clock=clock, do_push=False)
    finally:
        store_mod._atomic_write_text = real

    assert not queue.is_consolidated("s1")

    second = copy.deepcopy(base_facts())
    second["languages"]["fr"]["goals"] = "reworded by the re-run model"
    report = run_drain(
        store,
        adapter,
        StubDistiller(
            proposal=Proposal(
                facts=second,
                entries={"errors/fr": [{"id": "fr-deux-erreur", "pattern": "deux erreur"}]},
            )
        ),
        queue,
        clock=clock,
        do_push=False,
    )

    assert report.consolidated == ["s1"]
    assert queue.is_consolidated("s1")


def test_a_byte_identical_replay_survives_a_json_round_trip(store):
    """`same_content_as` compared live objects to round-tripped ones, so a tuple, a non-string dict
    key, or a NaN turned crash recovery into a hard error."""
    entries = [{"id": "e1", "tags": ("a", "b"), "counts": {1: "x"}}]
    store.append("vocab/fr", entries, session="s1", batch="b1", )
    store.append("vocab/fr", copy.deepcopy(entries), session="s1", batch="b1")

    assert len(store.log("vocab/fr").read()) == 1


def test_a_dotted_key_cannot_borrow_another_paths_tombstone():
    """`("a.b", "c")` and `("a", "b", "c")` used to render the same marker, so one tombstone
    authorised a deletion at a path the operator never named."""
    from memento.gates import path_marker

    dotted = path_marker(("a.b", "c"))
    nested = path_marker(("a", "b", "c"))
    assert dotted != nested

    current = {"a.b": {"c": "one"}, "a": {"b": {"c": "two"}}}
    proposed = {"a.b": {}, "a": {"b": {}}}

    violations = floor_violations(current, proposed, tombstones={dotted})

    assert [v.path for v in violations] == [nested]  # only the one that was retired is allowed


def test_unverifiable_facts_are_refused_on_the_way_in(store, adapter):
    """They used to write cleanly to an empty store and then fail every later proposal — including
    an exact no-op — which, with all-or-nothing writes, locked the store for good."""
    facts = {"blob": [{"kind": "note"}]}  # no id/topic/name: the floor cannot address it

    with pytest.raises(GateFailure, match="cannot be stored"):
        apply_consolidation(
            store, adapter, Proposal(facts=facts), session="s1", batch="b1",
            expected_fingerprint=UNCHECKED,
        )
    assert read_facts(store) == {}


def test_widening_identity_keys_cannot_blind_the_floor(store):
    """An adapter's extra keys are searched *after* the engine's, never before."""
    adapter = Adapter(name="wide", identity_keys=("engagement", "id", "topic", "name"))
    current = {"interests": [{"topic": "lighthouses", "engagement": "low"}]}
    apply_consolidation(
        store, adapter, Proposal(facts=current), session="s1", batch="b1",
        expected_fingerprint=UNCHECKED,
    )

    swapped = {"interests": [{"topic": "crocheting", "engagement": "low"}]}
    with pytest.raises(GateFailure, match="interests/lighthouses"):
        apply_consolidation(
            store, adapter, Proposal(facts=swapped), session="s2", batch="b2",
            expected_fingerprint=UNCHECKED,
        )


def test_a_failing_backup_commit_does_not_strand_the_drain(store, adapter, queue, clock):
    from memento import backup as backup_mod

    backup_mod.enable_backup(store, acknowledged=True)
    queue.close_and_enqueue("s1")
    queue.close_and_enqueue("s2")

    real = backup_mod.commit_consolidation

    def explode(*a, **kw):
        raise RuntimeError("index.lock exists")

    backup_mod.commit_consolidation = explode
    try:
        report = run_drain(
            store, adapter, StubDistiller(proposal=Proposal(facts=base_facts())), queue,
            clock=clock, do_push=False,
        )
    finally:
        backup_mod.commit_consolidation = real

    assert report.consolidated == ["s1", "s2"]  # both written, both marked
    assert any(f.kind == "backup-failed" for f in report.flags)


def test_a_credential_bearing_remote_is_refused_before_anything_is_configured(store):
    from memento import BackupError, enable_backup

    with pytest.raises(BackupError, match="inline credential"):
        enable_backup(
            store,
            acknowledged=True,
            remote="https://x-access-token:ghp_" + "a" * 36 + "@github.com/o/r.git",
        )

    assert not (store.root / ".git").exists()  # and not half-configured either


def test_rollback_can_restore_content_that_trips_the_secrets_gate(store):
    """An adopted document may hold anything. Rollback is the operator's only recovery lever, so
    the gate must not be what takes it away."""
    store.replace_document("profile.md", "ordinary\n", session="s1", batch="b1")
    store.replace_document(
        "profile.md", "AKIA" + "IOSFODNN7EXAMPLE" + "\n", session="s2", batch="b2",
        scan_secrets=False,
    )

    from memento.forgetting import rollback_document

    store.replace_document("profile.md", "later\n", session="s3", batch="b3")
    rollback_document(store, "profile.md", session="s4", batch="b4")

    assert "IOSFODNN7EXAMPLE" in store.read_document("profile.md")


def test_a_legacy_claim_file_does_not_kill_the_drain(store, adapter, queue, clock):
    """`stale_claims` rebuilt a session id from a filename and handed it to a validator that had
    not existed when the file was written."""
    store.locks_dir.mkdir(parents=True, exist_ok=True)
    (store.locks_dir / "claim-2026-07-31T12:00:00.lock").write_text("{}", encoding="utf-8")
    queue.close_and_enqueue("s1")

    report = run_drain(
        store, adapter, StubDistiller(proposal=Proposal(facts=base_facts())), queue,
        clock=clock, do_push=False,
    )
    assert report.consolidated == ["s1"]


def test_a_queue_inside_the_store_is_never_pushed(store, tmp_path):
    """Wherever the consumer put it. The engine recognises its own queue by shape."""
    from memento import Queue, backup as backup_mod

    queue = Queue(store.root / "sessions-data")
    queue.append_turn("s1", 1, {"said": "verbatim transcript material"})
    backup_mod.enable_backup(store, acknowledged=True, queue_root=queue.root)
    store.replace_document("profile.md", "something to commit\n", session="s1", batch="b1")
    backup_mod.commit_consolidation(store, "s1")

    tracked = backup_mod.git(store, "ls-files").stdout
    assert "profile.md" in tracked
    assert "sessions-data" not in tracked


def test_the_prefix_trims_rather_than_dropping_a_whole_section(store):
    """A one-token overflow used to cost an entire section, because the re-truncation allowance
    ignored what the earlier parts had already spent."""

    class Merging:
        name = "merging"
        is_local = True

        def count(self, text: str) -> int:
            return len(text) + (1 if "a\n\nb" in text else 0)

    adapter = make_adapter(
        token_counter=Merging(),
        prefix_budget_tokens=9,
        prefix_sections=(
            PrefixSection(name="a", priority=0, render=lambda s: "aaaa"),
            PrefixSection(name="b", priority=1, render=lambda s: "b\nb\nb"),
        ),
    )

    result = assemble_prefix(store, adapter)

    assert result.dropped == []
    assert result.truncated == ["b"]
    assert Merging().count(result.text) <= 9


def test_an_abandoned_revision_is_retired_by_its_own_event(store, monkeypatch):
    """A crash between the event and the file swap leaves a revision that never happened.

    Retiring it inline on the *successor* was the half-measure: a reader that did not know to look
    for that key still saw a chain whose links did not meet, and rollback still offered a version
    the document never held. Supersession is an event everywhere else in this store; it is one here.
    """
    import memento.store as store_mod
    from memento.events import EVENT_DOCUMENT_WRITE_ABANDONED

    store.replace_document("profile.md", "v1\n", session="s0", batch="b0")

    real = store_mod._atomic_write_text
    monkeypatch.setattr(
        store_mod, "_atomic_write_text", lambda *a: (_ for _ in ()).throw(RuntimeError("cut"))
    )
    with pytest.raises(RuntimeError):
        store.replace_document("profile.md", "v2\n", session="s1", batch="b1")
    monkeypatch.setattr(store_mod, "_atomic_write_text", real)

    assert store.read_document("profile.md") == "v1\n"  # the swap never landed

    store.replace_document("profile.md", "v2 reworded\n", session="s1", batch="b1")

    log = store.document_log().read()
    retirements = [e for e in log if e.event == EVENT_DOCUMENT_WRITE_ABANDONED]
    assert len(retirements) == 1
    assert retirements[0].payload["document"] == "profile.md"

    # The chain now reads correctly to something that knows only the event kinds.
    live = document_revisions(store, "profile.md")
    assert [r.event.payload["prior_sha256"] for r in live][1:] == [
        r.event.payload["new_sha256"] for r in live
    ][:-1]

    # The abandoned revision is kept — nothing is deleted — but it is not a rollback target,
    # because it describes a state the document never held.
    assert [r.abandoned for r in live] == [False, False]
    everything = document_revisions(store, "profile.md", include_abandoned=True)
    assert [r.abandoned for r in everything] == [False, True, False]

    rollback_document(store, "profile.md", session="s2", batch="b2")
    assert store.read_document("profile.md") == "v1\n"  # the version that really existed


def test_facts_that_nest_too_deep_fail_closed_instead_of_blowing_the_stack(store, adapter):
    """An unbounded walk turned a deep proposal into `RecursionError` — not a `MementoError`, so no
    caller could catch it as one, and the drain's handler turned it into a permanent deferral."""
    from memento.gates import MAX_FACTS_DEPTH

    deep: dict = {}
    node = deep
    for _ in range(MAX_FACTS_DEPTH + 50):
        node["k"] = {}
        node = node["k"]

    with pytest.raises(GateFailure, match="nests deeper"):
        apply_consolidation(
            store, adapter, Proposal(facts=deep), session="s1", batch="b1",
            expected_fingerprint=UNCHECKED,
        )
    assert read_facts(store) == {}  # and it never reaches disk, so nothing is locked afterwards


def test_a_tree_at_the_depth_limit_is_still_accepted(store, adapter):
    from memento.gates import MAX_FACTS_DEPTH

    deep: dict = {}
    node = deep
    for _ in range(MAX_FACTS_DEPTH - 2):
        node["k"] = {}
        node = node["k"]

    assert apply_consolidation(
        store, adapter, Proposal(facts=deep), session="s1", batch="b1",
        expected_fingerprint=UNCHECKED,
    ).ok
