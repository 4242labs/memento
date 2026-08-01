"""Round-two adversarial repros. Each test asserts the CURRENT (broken) behaviour."""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

import pytest

from memento import (
    Adapter,
    MemoryStore,
    Proposal,
    Queue,
    StoreLock,
    StoreState,
    apply_consolidation,
    assemble_prefix,
    run_drain,
)
from memento.errors import (
    BudgetError,
    CorruptStoreError,
    LockTimeout,
    MementoError,
    SecretsDetected,
)
from memento.events import EventLog
from memento.gates import RuleSet, Violation
from memento.writepath import UNCHECKED, facts_fingerprint, read_facts
from conftest import base_facts, make_adapter, StubDistiller


# =====================================================================  R1
def test_R1_redrive_with_different_entries_wedges_the_session_forever(tmp_path):
    """run_drain uses batch=f'drain-{session}'. If run 1 wrote entries and then died before the
    marker, run 2's (legitimately different) distiller output hits append_batch's new
    content-mismatch check and raises -> deferred, forever. store.py's own docstring calls this
    'how a session becomes permanently unconsolidatable' and then does it for entry streams."""
    from memento import FrozenClock

    clock = FrozenClock()
    store = MemoryStore(tmp_path / "memory", clock=clock)
    store.initialize()
    queue = Queue(tmp_path / "q", clock=clock)
    adapter = make_adapter()
    queue.close_and_enqueue("s1")

    facts = base_facts()
    eid = "fr-je-suis-20-ans"
    p1 = Proposal(facts=facts, entries={
        "errors/fr": [{"id": eid, "pattern": "je suis 20 ans", "note": "first phrasing"}]})
    d1 = StubDistiller(proposal=p1)

    # run 1: crash right after the store write, before mark_consolidated
    real_mark = queue.mark_consolidated
    def boom(session):
        raise RuntimeError("power cut before the marker")
    queue.mark_consolidated = boom
    with pytest.raises(RuntimeError):
        run_drain(store, adapter, d1, queue, max_sessions=1)
    queue.mark_consolidated = real_mark

    # entries landed
    assert [e.id for e in store.log("errors/fr").read()] == [eid]
    assert not queue.is_consolidated("s1")

    # run 2: the model, re-run, rewords its note. Same entry, same derived id. Ordinary, per store.py.
    p2 = Proposal(facts=facts, entries={
        "errors/fr": [{"id": eid, "pattern": "je suis 20 ans", "note": "second phrasing"}]})
    d2 = StubDistiller(proposal=p2)
    for attempt in range(3):
        report = run_drain(store, adapter, d2, queue, max_sessions=1)
        assert report.consolidated == [], f"attempt {attempt} unexpectedly succeeded"
        assert report.deferred, f"attempt {attempt}"
        assert "already recorded with different entries" in report.deferred[0][1]
    # permanently stuck
    assert not queue.is_consolidated("s1")


# =====================================================================  R2
def test_R2_commit_consolidation_failure_escapes_run_drain(tmp_path, monkeypatch):
    """The new blanket `except Exception` only wraps apply_consolidation. A failure in
    commit_consolidation (git missing, repo locked, disk full) escapes run_drain entirely,
    strands every session behind it, and leaves this one written-but-unmarked -> R1."""
    from memento import FrozenClock
    from memento import backup as backup_mod

    clock = FrozenClock()
    store = MemoryStore(tmp_path / "memory", clock=clock)
    store.initialize()
    queue = Queue(tmp_path / "q", clock=clock)
    adapter = make_adapter()
    queue.close_and_enqueue("s1")
    queue.close_and_enqueue("s2")

    monkeypatch.setattr(backup_mod, "is_enabled", lambda store: True)
    def exploding_commit(store, session, *, lock=None):
        raise backup_mod.BackupError("git commit failed: index.lock exists")
    monkeypatch.setattr(backup_mod, "commit_consolidation", exploding_commit)

    d = StubDistiller(proposal=Proposal(facts=base_facts()))
    with pytest.raises(backup_mod.BackupError):
        run_drain(store, adapter, d, queue)

    assert not queue.is_consolidated("s1")          # written but unmarked
    assert store.read_document("profile.md")        # ... and the write DID land
    assert not queue.is_consolidated("s2")          # s2 never even ran


# =====================================================================  R3
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"tags": ("a", "b")}, id="tuple"),
        pytest.param({"counts": {1: "x"}}, id="int-dict-key"),
        pytest.param({"score": float("nan")}, id="nan"),
    ],
)
def test_R3_identical_replay_raises_because_same_content_as_compares_pre_json(tmp_path, payload):
    """append_batch compares in-memory entries against JSON-round-tripped ones. A payload whose
    JSON round-trip is not identity turns an *identical* replay -- the crash-recovery case D2
    calls idempotent -- into a hard MementoError."""
    log = EventLog(tmp_path / "a.jsonl")
    entry = {"id": "x", **payload}
    log.append_batch([dict(entry)], session="s", batch="b")
    with pytest.raises(MementoError, match="already recorded with different entries"):
        log.append_batch([dict(entry)], session="s", batch="b")   # byte-identical replay


# =====================================================================  R4
def test_R4_tombstone_marker_collides_across_paths_and_authorizes_real_erosion():
    """path_marker joins the parent path with '.', so ('a.b','c') and ('a','b','c') produce the
    SAME marker 'a.b/c'. Tombstoning one authorizes dropping the other -- the exact dotted-key
    ambiguity gates.py's module docstring claims to have removed."""
    from memento.gates import path_marker

    assert path_marker(("a.b", "c")) == path_marker(("a", "b", "c")) == "a.b/c"

    current = StoreState(
        facts={
            "a.b": {"c": "flat value"},          # the one the operator forgot
            "a": {"b": {"c": "nested value"}},   # a completely different fact
        }
    )
    proposal = Proposal(
        facts={"a.b": {}, "a": {"b": {}}},       # BOTH dropped
        tombstones={"a.b/c"},                    # only ONE authorized
    )
    violations = RuleSet().check(current, proposal)
    assert violations == [], f"expected the erosion to slip through, got {violations}"


# =====================================================================  R5
@pytest.mark.parametrize(
    "value",
    [
        pytest.param([[1, 2], [3, 4]], id="nested-list"),
        pytest.param([None], id="none-member"),
        pytest.param([{"kind": "note"}], id="no-identity-key"),
    ],
)
def test_R5_unverifiable_facts_write_once_then_lock_the_store_forever(tmp_path, value):
    """The floor only walks *current* facts, so an unverifiable collection writes fine on an empty
    store -- and then every subsequent consolidation, including an exact no-op, fails closed.
    Writes are all-or-nothing, so the store is wedged."""
    store = MemoryStore(tmp_path / "memory")
    store.initialize()
    adapter = Adapter(name="a")   # empty adapter: floor only

    facts = {"blob": value}
    apply_consolidation(store, adapter, Proposal(facts=facts), session="s1", batch="b1",
                        expected_fingerprint=UNCHECKED)
    assert read_facts(store) == json.loads(json.dumps(facts))

    # an exact no-op re-proposal of what is already on disk
    same = read_facts(store)
    from memento.errors import GateFailure
    with pytest.raises(GateFailure) as exc:
        apply_consolidation(store, adapter, Proposal(facts=same), session="s2", batch="b2",
                            expected_fingerprint=facts_fingerprint(same))
    assert "cannot be verified" in exc.value.render()


# =====================================================================  R6
def test_R6_enable_backup_is_blocked_by_its_own_secrets_gate_after_git_init(tmp_path):
    """A token-in-URL remote is the ordinary unattended-push setup. The new store-level secrets
    gate now rejects the backup config -- but only AFTER git init + remote add have run, leaving
    a half-configured repo that reports backup disabled."""
    from memento.backup import enable_backup, is_enabled

    store = MemoryStore(tmp_path / "memory")
    store.initialize()
    token = "ghp_" + "A" * 36
    remote = f"https://x-access-token:{token}@github.com/me/private-memory.git"

    with pytest.raises(SecretsDetected):
        enable_backup(store, acknowledged=True, remote=remote)

    assert (store.root / ".git").exists()      # git init happened
    assert not is_enabled(store)               # ... but backup is off
    import subprocess
    out = subprocess.run(["git", "remote", "-v"], cwd=store.root, capture_output=True, text=True)
    assert "origin" in out.stdout              # the remote is configured behind the operator's back


# =====================================================================  R7
def test_R7_rollback_is_blocked_by_the_secrets_gate(tmp_path):
    """A pre-engine store (the stated compatibility target) can hold a document that trips the
    secrets patterns. document_replaced records it as prior content, and rollback -- the operator's
    only recovery lever, ADR D2 -- now refuses to restore it."""
    from memento.forgetting import rollback_document

    store = MemoryStore(tmp_path / "memory")
    store.initialize()
    leaked = "notes\nAKIA" + "B" * 16 + "\n"
    (store.root / "profile.md").write_text(leaked, encoding="utf-8")   # pre-existing content

    store.replace_document("profile.md", "clean\n", session="s1", batch="b1")
    assert store.document_history("profile.md")[0].payload["prior_content"] == leaked

    with pytest.raises(SecretsDetected):
        rollback_document(store, "profile.md", session="s2", batch="b2")
    assert store.read_document("profile.md") == "clean\n"   # prior content unrecoverable


# =====================================================================  R8
def test_R8_storelock_registry_silently_ignores_a_caller_supplied_timeout(tmp_path):
    """__new__ returns the cached instance and __init__ is a no-op, so the *second* caller's
    timeout is discarded. A caller asking for a 0.05s fail-fast silently gets whoever-was-first's
    30s -- and it is not even deterministic which one wins."""
    locks = tmp_path / "locks"
    locks.mkdir()
    a = StoreLock(locks, timeout=30.0)
    b = StoreLock(locks, timeout=0.05)
    assert a is b
    assert b.timeout == 30.0          # the caller asked for 0.05


def test_R8b_registry_is_never_evicted(tmp_path):
    """Class-level dict keyed by resolved path, no eviction. A long-lived process touching many
    stores leaks one StoreLock (plus an RLock) per store, forever."""
    before = len(StoreLock._instances)
    for i in range(200):
        d = tmp_path / f"s{i}"
        d.mkdir()
        StoreLock(d)
    assert len(StoreLock._instances) - before == 200


def test_R8c_two_store_roots_that_are_symlinks_to_one_dir_share_a_lock(tmp_path):
    """resolve() is the registry key, so two *different* store roots pointing at one directory
    share a lock. Correct here. But the inverse also holds: bind-mount / separate symlink paths
    to DIFFERENT dirs never share -- documented for completeness."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert StoreLock(real) is StoreLock(link)


def test_R8d_forked_child_inherits_a_held_lock_and_skips_flock(tmp_path):
    """The registry (and _depth) survive os.fork(). A child forked while the parent holds the
    store lock believes it already holds it: hold() takes the reentrant branch and never touches
    the flock, so the child writes inside the parent's critical section."""
    import os

    locks = tmp_path / "locks"
    locks.mkdir()
    lock = StoreLock(locks)
    r, w = os.pipe()
    with lock.hold():
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                child = StoreLock(locks, timeout=0.2)
                took_flock = "no"
                with child.hold():
                    # if this is the reentrant branch, _fd is the *parent's* fd number
                    took_flock = "reentrant" if child._depth == 2 else "flock"
                os.write(w, took_flock.encode())
            finally:
                os._exit(0)
        os.close(w)
        result = os.read(r, 32).decode()
        os.waitpid(pid, 0)
    assert result == "reentrant", result


# =====================================================================  R9
def test_R9_event_objects_bypass_the_store_secrets_gate(tmp_path):
    """store.append scans dict entries and explicitly skips Event instances, so the 'last door
    before disk' has a hinge missing. ADR D8: 'Secrets never enter the store'."""
    from memento.events import Event

    store = MemoryStore(tmp_path / "memory")
    store.initialize()
    secret = "AKIA" + "C" * 16
    store.append("notes", [Event(event="entry", session="s", batch="b", ordinal=0, ts="t",
                                 id="x", payload={"aws": secret})],
                 session="s", batch="b")
    assert secret in (store.root / "notes.jsonl").read_text(encoding="utf-8")


# =====================================================================  R10
def test_R10_a_scalar_json_line_raises_AttributeError_and_repair_preserves_it(tmp_path):
    """Event.from_obj advertises itself as permissive but calls .items() unguarded. A log line
    that is valid JSON and not an object crashes read() with AttributeError -- not the documented
    CorruptStoreError -- and _repair_tail *keeps* such a line because json.loads succeeded."""
    p = tmp_path / "a.jsonl"
    p.write_bytes(b"123")                       # no trailing newline: repair territory
    log = EventLog(p)
    with pytest.raises(AttributeError):
        log.read()
    log._repair_tail()
    assert p.read_bytes() == b"123\n"           # repair preserved the poison
    with pytest.raises(AttributeError):
        log.read()


# =====================================================================  R11
def test_R11_stale_claims_raises_on_a_claim_file_it_cannot_validate(tmp_path):
    """stale_claims reconstructs a session id from a filename and feeds it to SessionClaim, which
    now validates. One stray/legacy claim file and every drain dies on its first line -- before
    any session is even looked at."""
    from memento.locking import stale_claims

    locks = tmp_path / "locks"
    locks.mkdir()
    (locks / "claim-2026-07-31T12:00:00.lock").write_text("{}", encoding="utf-8")
    with pytest.raises(MementoError):
        stale_claims(locks, now=0.0)


def test_R11b_run_drain_dies_on_that_stray_claim_file(tmp_path):
    from memento import FrozenClock
    clock = FrozenClock()
    store = MemoryStore(tmp_path / "memory", clock=clock)
    store.initialize()
    queue = Queue(tmp_path / "q", clock=clock)
    queue.close_and_enqueue("s1")
    (store.locks_dir / "claim-2026-07-31T12:00:00.lock").write_text("{}", encoding="utf-8")
    with pytest.raises(MementoError):
        run_drain(store, make_adapter(), StubDistiller(proposal=Proposal(facts={})), queue)


# =====================================================================  R12
def test_R12_adapter_can_still_weaken_the_floor_via_identity_keys():
    """The adapter docstring says 'widen it when your taxonomy identifies members some other way'.
    member_key returns the FIRST declared key present, so a widened tuple whose new key comes first
    makes the floor blind to substitution. ADR D3.1: adapters tighten, never disable."""
    from memento.gates import DEFAULT_IDENTITY_KEYS

    current = StoreState(facts={"interests": [
        {"id": "kite-building", "engagement": "medium"},
        {"id": "lighthouses", "engagement": "low"},
    ]})
    # 'lighthouses' is silently replaced by a brand-new interest. No tombstone.
    proposal = Proposal(facts={"interests": [
        {"id": "kite-building", "engagement": "medium"},
        {"id": "crocheting", "engagement": "low"},
    ]})

    strict = RuleSet(identity_keys=DEFAULT_IDENTITY_KEYS).check(current, proposal)
    assert strict, "the default floor must catch this"

    widened = ("engagement", *DEFAULT_IDENTITY_KEYS)   # a *superset*, following the docs
    assert RuleSet(identity_keys=widened).check(current, proposal) == []


# =====================================================================  R12b
def test_R12b_redrive_breaks_the_document_replaced_chain(tmp_path):
    """_write_batch_id turns a differing redrive into a new revision. The abandoned first event
    stays in the log claiming a transition that never happened, so document_history no longer
    chains: rev1.prior_sha256 != rev0.new_sha256. ADR D2 calls this log the audit history."""
    store = MemoryStore(tmp_path / "memory")
    store.initialize()
    (store.root / "profile.md").write_text("v0\n", encoding="utf-8")

    log = store.document_log()
    # simulate the crash window: append the event, die before the file swap
    from memento.store import _write_batch_id, sha256_text
    wb = _write_batch_id("b1", ["profile.md"], ["vA\n"])
    log.append_batch([{ "event": "document_replaced", "document": "profile.md",
                        "new_sha256": sha256_text("vA\n"), "prior_sha256": sha256_text("v0\n"),
                        "prior_content": "v0\n"}], session="s1", batch=wb)

    # redrive, different model output
    store.replace_document("profile.md", "vB\n", session="s1", batch="b1")

    hist = store.document_history("profile.md")
    assert len(hist) == 2
    assert hist[0].payload["new_sha256"] == sha256_text("vA\n")     # never landed
    assert hist[1].payload["prior_sha256"] == sha256_text("v0\n")   # chain broken
    assert hist[1].payload["prior_sha256"] != hist[0].payload["new_sha256"]


# =====================================================================  R12c
def test_R12c_deeply_nested_facts_write_once_then_blow_the_recursion_limit(tmp_path):
    """Same shape as R5: the floor only walks *current* facts, so deep nesting writes fine and
    then RecursionError-s every later consolidation. Not a MementoError -- callers cannot catch it
    as one, and run_drain's blanket handler turns it into a permanent deferral."""
    store = MemoryStore(tmp_path / "memory")
    store.initialize()
    adapter = Adapter(name="a")

    deep: dict = {}
    node = deep
    for i in range(400):
        node["k"] = {}
        node = node["k"]

    apply_consolidation(store, adapter, Proposal(facts=deep), session="s1", batch="b1",
                        expected_fingerprint=UNCHECKED)
    same = read_facts(store)
    with pytest.raises(RecursionError):
        apply_consolidation(store, adapter, Proposal(facts=same), session="s2", batch="b2",
                            expected_fingerprint=facts_fingerprint(same))


# =====================================================================  R13
def test_R13_prefix_retruncation_loop_can_raise_StopIteration(tmp_path):
    """The `next(...)` in the re-truncation loop has no default. A counter that returns 0 for a
    short non-empty string (a word-counting tokenizer on a punctuation-only section) leaves every
    result with tokens == 0 while the joined whole overflows -> StopIteration off the hot path."""
    from memento import PrefixSection

    store = MemoryStore(tmp_path / "memory")
    store.initialize()

    class ShortIsFree:
        name = "short-is-free"
        is_local = True
        def count(self, text: str) -> int:
            return 0 if len(text) <= 3 else len(text)

    adapter = make_adapter(
        token_counter=ShortIsFree(),
        prefix_budget_tokens=5,
        prefix_sections=(
            PrefixSection(name="a", priority=0, render=lambda s: "ab"),
            PrefixSection(name="b", priority=1, render=lambda s: "cd"),
        ),
    )
    with pytest.raises(StopIteration):
        assemble_prefix(store, adapter)


# =====================================================================  R14
def test_R14_write_session_log_takes_an_unvalidated_session_id(tmp_path):
    """store.write_session_log / read_session_log build a path straight from the session id with
    no validate_session_id. _resolve blocks escapes, but a slash-bearing id silently creates a
    directory tree the session_logs() listing can never see again."""
    store = MemoryStore(tmp_path / "memory")
    store.initialize()
    store.write_session_log("a/b", "hello")
    assert (store.root / "sessions" / "log-a" / "b.md").exists()
    assert store.session_logs() == []          # invisible to the listing
