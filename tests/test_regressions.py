"""Regressions from the adversarial review of the first build.

Every test here corresponds to a defect that shipped in the first implementation and passed a
172-test suite written by the same author. They are collected in one file on purpose: this is the
list of things that were confidently believed and were wrong, and it is worth reading as such.

Each one asserts the *corrected* behaviour.
"""

from __future__ import annotations

import copy
import json
import threading

import pytest

from conftest import StubDistiller, base_facts, make_adapter
from memento import (
    Adapter,
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


def test_the_lock_depth_cannot_underflow(store):
    """It used to reach -1, and -1 is truthy: the lock became a permanent no-op."""
    lock = StoreLock(store.locks_dir, timeout=0.3)
    for _ in range(3):
        try:
            with lock.hold(timeout=0.3):
                pass
        except Exception:  # pragma: no cover - defensive
            pass
    assert lock._depth == 0

    with lock.hold():
        assert lock._fd is not None  # still really taking the flock


def test_two_threads_are_never_inside_the_critical_section_at_once(store):
    lock = StoreLock(store.locks_dir, timeout=2.0)
    inside: list[str] = []
    overlaps: list[bool] = []

    def worker(name):
        with lock.hold():
            overlaps.append(len(inside) > 0)
            inside.append(name)
            threading.Event().wait(0.05)
            inside.remove(name)

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert overlaps == [False, False, False, False]


# ================================================== M7 — a replayed batch is not a silent drop


def test_reusing_a_batch_id_for_different_entries_is_refused(store, adapter):
    """It used to discard the new entries and report `ok=True`, streams and all."""
    facts = base_facts()
    apply_consolidation(
        store,
        adapter,
        Proposal(facts=facts, entries={"errors/fr": [{"id": "fr-un-erreur", "pattern": "un erreur"}]}),
        session="s1",
        batch="b1",
        expected_fingerprint=UNCHECKED,
    )

    with pytest.raises(MementoError, match="already recorded with different entries"):
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
    outside = tmp_path / "other-store"
    outside.mkdir()
    (outside / "x.jsonl").write_text("", encoding="utf-8")
    (store.root / "link").symlink_to(outside, target_is_directory=True)

    assert "link/x" not in store.streams()


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
