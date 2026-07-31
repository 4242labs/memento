"""The store IS the namespace (ADR D1).

Isolation is architectural here, not policy: there is no `(agent, user)` tuple to get wrong and no
multi-tenant seam to leak through. These tests pin that shape so it cannot drift back.
"""

from __future__ import annotations

import inspect

import pytest

from memento import MementoError, MemoryStore


def test_the_constructor_takes_a_store_root_and_no_tenant_identity():
    params = list(inspect.signature(MemoryStore.__init__).parameters)
    assert params == ["self", "store_root", "clock", "inline_limit"]
    assert not any(p in params for p in ("agent", "user", "tenant", "namespace"))


def test_a_relative_path_cannot_escape_the_store(store):
    for escape in ("../elsewhere", "sessions/../../elsewhere", "a/b/../../../x"):
        with pytest.raises(MementoError, match="escapes the store root"):
            store.document_path(escape)


def test_an_absolute_path_is_refused(store):
    with pytest.raises(MementoError, match="relative to the store root"):
        store.document_path("/etc/passwd")


def test_a_stream_id_cannot_escape_either(store):
    with pytest.raises(MementoError, match="escapes the store root"):
        store.append("../outside", [{"id": "x"}], session="s1", batch="b1")


def test_two_stores_share_code_and_nothing_else(tmp_path, clock, adapter):
    from memento import Proposal, apply_consolidation
    from memento.writepath import UNCHECKED, read_facts

    from conftest import base_facts

    a = MemoryStore(tmp_path / "a", clock=clock)
    b = MemoryStore(tmp_path / "b", clock=clock)

    apply_consolidation(a, adapter, Proposal(facts=base_facts()), session="s1", batch="b1", expected_fingerprint=UNCHECKED)

    assert read_facts(a)["languages"]
    assert read_facts(b) == {}
    assert b.documents() == []
    assert b.streams() == []


def test_a_lock_in_one_store_does_not_block_another(tmp_path, clock):
    from memento import StoreLock

    a = MemoryStore(tmp_path / "a", clock=clock)
    b = MemoryStore(tmp_path / "b", clock=clock)
    a.initialize()
    b.initialize()

    with StoreLock(a.locks_dir, timeout=0.2).hold():
        with StoreLock(b.locks_dir, timeout=0.2).hold():
            pass  # different namespace, different lock
