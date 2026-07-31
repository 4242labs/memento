"""Optional git backup (ADR D8/D3.4): opt-in, attribution, and lock discipline."""

from __future__ import annotations

import json
import subprocess

import pytest

from conftest import StubDistiller, base_facts
from memento.writepath import UNCHECKED
from memento import BackupError, Proposal, Queue, StoreLock, apply_consolidation, run_drain
from memento import backup as backup_mod


def _bare_remote(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    return remote


def test_backup_is_off_until_someone_opts_in(store):
    assert not backup_mod.is_enabled(store)
    assert not (store.root / ".git").exists()


def test_enabling_without_acknowledgement_is_refused(store):
    with pytest.raises(BackupError, match="explicit opt-in"):
        backup_mod.enable_backup(store, acknowledged=False, remote="git@example.com:x/y.git")
    assert not (store.root / ".git").exists()


def test_enabling_records_the_warning_that_was_shown(store):
    backup_mod.enable_backup(store, acknowledged=True)
    config = json.loads(store.read_document(backup_mod.CONFIG_DOCUMENT))
    assert config["enabled"] is True
    assert "PRIVATE remote" in config["warning_shown"]


def test_the_queue_and_the_locks_are_never_pushed(store):
    backup_mod.enable_backup(store, acknowledged=True)
    ignored = (store.root / ".gitignore").read_text()
    assert ".memento/queue/" in ignored and ".memento/locks/" in ignored


def test_a_git_less_store_commits_nothing_and_says_so(store, adapter):
    apply_consolidation(store, adapter, Proposal(facts=base_facts()), session="s1", batch="b1", expected_fingerprint=UNCHECKED)
    assert backup_mod.commit_consolidation(store, "s1") is None


def test_each_consolidation_is_attributed_to_the_session_it_consolidated(store, adapter):
    """Never batched under a later session — that is the whole point of committing immediately."""
    backup_mod.enable_backup(store, acknowledged=True)

    apply_consolidation(store, adapter, Proposal(facts=base_facts()), session="s1", batch="b1", expected_fingerprint=UNCHECKED)
    backup_mod.commit_consolidation(store, "s1")

    grown = base_facts()
    grown["languages"]["pt"] = {"level": "A1", "confidence": "low", "goals": "read song lyrics"}
    apply_consolidation(store, adapter, Proposal(facts=grown), session="s2", batch="b2", expected_fingerprint=UNCHECKED)
    backup_mod.commit_consolidation(store, "s2")

    assert backup_mod.commit_messages(store)[:2] == [
        "memory: consolidate session s2",
        "memory: consolidate session s1",
    ]


def test_a_no_op_consolidation_makes_no_empty_commit(store, adapter):
    backup_mod.enable_backup(store, acknowledged=True)
    apply_consolidation(store, adapter, Proposal(facts=base_facts()), session="s1", batch="b1", expected_fingerprint=UNCHECKED)
    backup_mod.commit_consolidation(store, "s1")
    assert backup_mod.commit_consolidation(store, "s1") is None


def test_the_local_half_runs_under_the_lock_and_the_network_half_does_not(store, adapter, tmp_path):
    """Lock ordering, asserted per git subcommand: a hung push must not stall the other front-end."""
    remote = _bare_remote(tmp_path)
    backup_mod.enable_backup(store, acknowledged=True, remote=str(remote))
    apply_consolidation(store, adapter, Proposal(facts=base_facts()), session="s1", batch="b1", expected_fingerprint=UNCHECKED)

    lock = StoreLock(store.locks_dir)
    seen: list[tuple[str, bool]] = []
    real_git = backup_mod.git

    def recording_git(st, *args, **kwargs):
        seen.append((args[0], lock.held))
        return real_git(st, *args, **kwargs)

    backup_mod.git = recording_git
    try:
        backup_mod.commit_consolidation(store, "s1", lock=lock)
        backup_mod.push(store)
    finally:
        backup_mod.git = real_git

    held = dict(seen)
    assert held["add"] is True and held["commit"] is True
    assert held["pull"] is False and held["push"] is False


def test_every_git_call_carries_a_timeout(store):
    backup_mod.enable_backup(store, acknowledged=True)
    with pytest.raises(BackupError, match="timed out"):
        backup_mod.git(store, "log", timeout=0.0)


def test_a_drain_commits_when_backup_is_on(store, adapter, tmp_path, clock):
    backup_mod.enable_backup(store, acknowledged=True)
    queue = Queue(tmp_path / "sessions-data", clock=clock)
    queue.close_and_enqueue("s1")

    report = run_drain(
        store,
        adapter,
        StubDistiller(proposal=Proposal(facts=base_facts())),
        queue,
        clock=clock,
        do_push=False,
    )

    assert report.committed
    assert backup_mod.commit_messages(store)[0] == "memory: consolidate session s1"


def test_a_failing_push_flags_but_never_loses_the_local_write(store, adapter, tmp_path, clock):
    backup_mod.enable_backup(store, acknowledged=True, remote=str(tmp_path / "nowhere.git"))
    queue = Queue(tmp_path / "sessions-data", clock=clock)
    queue.close_and_enqueue("s1")

    report = run_drain(
        store, adapter, StubDistiller(proposal=Proposal(facts=base_facts())), queue, clock=clock
    )

    assert report.consolidated == ["s1"]
    assert queue.is_consolidated("s1")
    assert any(f.kind == "backup-failed" for f in report.flags)
