"""Round-two, part 2: vacuity of the regression suite + remaining probes."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from memento import (
    Adapter,
    MemoryStore,
    PrefixSection,
    Proposal,
    StoreLock,
    apply_consolidation,
    assemble_prefix,
)
from memento.writepath import UNCHECKED
from conftest import make_adapter
from old_lock import OldStoreLock


# ---------------------------------------------------------------- V1: M3 tests
def test_V1_the_unfixed_lock_really_is_broken_under_a_forced_interleaving(tmp_path):
    """The defect the M3 fix names is real -- but only visible when thread B enters hold() *after*
    thread A has set _depth. Neither regression test forces that, so neither one fails on the
    pre-fix code (see test_V1b)."""
    locks = tmp_path / "locks"
    locks.mkdir()
    lock = OldStoreLock(locks, timeout=2.0)
    a_inside = threading.Event()
    b_entered_without_flock = []

    def a():
        with lock.hold():
            a_inside.set()
            threading.Event().wait(0.3)

    def b():
        a_inside.wait(2)
        with lock.hold():
            b_entered_without_flock.append(lock._depth)   # 2 == reentrant branch, no flock

    ta, tb = threading.Thread(target=a), threading.Thread(target=b)
    ta.start(); tb.start(); ta.join(5); tb.join(5)
    assert b_entered_without_flock == [2]      # two threads inside the critical section


def test_V1b_the_shipped_M3_regression_test_passes_on_the_unfixed_lock(tmp_path):
    """tests/test_regressions.py::test_two_threads_are_never_inside_the_critical_section_at_once,
    verbatim, against the pre-fix implementation. It passes -> green means nothing here."""
    locks = tmp_path / "locks"
    locks.mkdir()
    lock = OldStoreLock(locks, timeout=2.0)
    inside, overlaps = [], []

    def worker(name):
        with lock.hold():
            overlaps.append(len(inside) > 0)
            inside.append(name)
            threading.Event().wait(0.05)
            inside.remove(name)

    ts = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(4)]
    for t in ts: t.start()
    for t in ts: t.join(timeout=10)
    assert overlaps == [False, False, False, False]     # the shipped assertion, on broken code


# ---------------------------------------------------------------- V2: symlink test
def test_V2_the_symlink_regression_test_is_vacuous_on_this_python(tmp_path):
    """Path.rglob does not descend into directory symlinks (3.12/3.13), so 'link/x' was never in
    streams() before the fix either. The test asserts something that was already true."""
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    (outside / "x.jsonl").write_text("", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)
    assert list(root.rglob("*.jsonl")) == []     # nothing for the new resolve() check to filter


# ---------------------------------------------------------------- P1: trim branch
def test_P1_the_retruncation_loop_never_trims_it_only_drops(tmp_path):
    """The re-truncation allowance is `budget - count(SEP)` -- the WHOLE budget, ignoring what the
    earlier parts already spend. Any last section that fits the budget on its own therefore comes
    back unchanged and falls into the pop branch. The 'trim from the least important end' the
    comment promises is unreachable in the common case: a 1-token overflow costs a whole section."""
    store = MemoryStore(tmp_path / "memory")
    store.initialize()

    class Merging:
        name = "merging"
        is_local = True
        def count(self, text: str) -> int:
            return len(text) + (1 if "a\n\nb" in text else 0)

    big = "b" * 3
    adapter = make_adapter(
        token_counter=Merging(),
        prefix_budget_tokens=9,
        prefix_sections=(
            PrefixSection(name="a", priority=0, render=lambda s: "aaaa"),
            PrefixSection(name="b", priority=1, render=lambda s: "b\nb\nb"),
        ),
    )
    result = assemble_prefix(store, adapter)
    # one token over -> the entire second section is gone, though dropping one line would do
    assert result.dropped == ["b"]
    assert result.truncated == []
    assert result.text == "aaaa"


# ---------------------------------------------------------------- P2: unlocked writes
def test_P2_operator_forget_appends_to_the_store_outside_the_store_lock(tmp_path):
    """forgetting.tombstone / retire_entry / note_contradiction call store.append directly. No
    lock is taken, so they race a drain's writes to the same stream. ADR D3.3: one write lock
    serializing consolidation writes."""
    import inspect
    from memento import forgetting

    for fn in (forgetting.tombstone, forgetting.retire_entry, forgetting.note_contradiction):
        src = inspect.getsource(fn)
        assert "lock" not in src, fn.__name__

    # and forget_fact writes its tombstone before entering apply_consolidation's lock
    src = inspect.getsource(forgetting.forget_fact)
    assert src.index("tombstone(store") < src.index("apply_consolidation")


# ---------------------------------------------------------------- P3: fingerprint sanity
def test_P3_fingerprint_CAS_is_inside_the_lock_and_does_hold(tmp_path):
    """Control: the compare-and-swap really is re-read under the lock, and non-ASCII / float /
    key-order variation does not produce a spurious StaleProposal. This one I could not break."""
    from memento.writepath import facts_fingerprint, read_facts

    store = MemoryStore(tmp_path / "memory")
    store.initialize()
    adapter = Adapter(name="a")
    facts = {"z": 1, "a": {"ü": 0.1 + 0.2, "n": [{"id": "x"}]}, "m": "café é"}
    apply_consolidation(store, adapter, Proposal(facts=facts), session="s1", batch="b1",
                        expected_fingerprint=UNCHECKED)
    back = read_facts(store)
    assert facts_fingerprint(back) == facts_fingerprint(facts)
    # reordered dict literal, same content
    reordered = {"m": "café é", "a": {"n": [{"id": "x"}], "ü": 0.1 + 0.2}, "z": 1}
    assert facts_fingerprint(reordered) == facts_fingerprint(facts)
    assert apply_consolidation(store, adapter, Proposal(facts=reordered), session="s2", batch="b2",
                               expected_fingerprint=facts_fingerprint(back)).ok
