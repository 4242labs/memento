"""Secrets never enter the store (ADR D8) — a write gate, not an afterthought."""

from __future__ import annotations

import pytest

from conftest import base_facts
from memento.writepath import UNCHECKED
from memento import Proposal, SecretsDetected, apply_consolidation
from memento.flags import SECRETS_REJECTED, FlagSink
from memento.secrets import scan
from support.fake_credentials import fake_credential

@pytest.mark.parametrize(
    "kind",
    ["aws-access-key", "github-token", "anthropic-key", "private-key-block", "assigned-secret"],
)
def test_each_pattern_is_detected(kind):
    assert kind in {m.pattern for m in scan(fake_credential(kind))}


def test_ordinary_prose_is_not_a_secret():
    assert scan("The learner said they work in software and enjoy films.") == []


def test_a_secret_in_a_document_is_refused_and_nothing_is_written(seeded, adapter, queue):
    before = seeded.read_document("profile.md")
    sink = FlagSink()
    proposal = Proposal(
        facts=base_facts(),
        documents={"profile.md": f"## en\n- token: {fake_credential('github-token')}\n"},
    )

    with pytest.raises(SecretsDetected):
        apply_consolidation(
            seeded, adapter, proposal, session="s2", batch="b2", queue=queue, sink=sink,
            expected_fingerprint=UNCHECKED,
        )

    assert seeded.read_document("profile.md") == before
    assert [f.kind for f in sink.flags] == [SECRETS_REJECTED]


def test_a_secret_in_an_entry_is_refused(seeded, adapter):
    proposal = Proposal(
        facts=base_facts(),
        entries={"vocab/en": [{"id": "v1", "item": fake_credential("aws-access-key")}]},
    )
    with pytest.raises(SecretsDetected):
        apply_consolidation(seeded, adapter, proposal, session="s2", batch="b2", expected_fingerprint=UNCHECKED)


def test_a_secret_in_the_session_log_is_refused(seeded, adapter):
    proposal = Proposal(
        facts=base_facts(),
        session_log=f"they pasted {fake_credential('private-key-block')} into the chat",
    )
    with pytest.raises(SecretsDetected):
        apply_consolidation(seeded, adapter, proposal, session="s2", batch="b2", expected_fingerprint=UNCHECKED)


def test_a_secret_in_the_facts_is_refused(seeded, adapter):
    facts = base_facts()
    facts["languages"]["fr"]["goals"] = f"remember my token {fake_credential('github-token')}"
    with pytest.raises(SecretsDetected):
        apply_consolidation(seeded, adapter, Proposal(facts=facts), session="s2", batch="b2", expected_fingerprint=UNCHECKED)


def test_the_gate_fires_before_anything_touches_disk(store, adapter, queue):
    """Rejection order matters: a secret must not reach the store even transiently."""
    queue.close_and_enqueue("s1")
    with pytest.raises(SecretsDetected):
        apply_consolidation(
            store,
            adapter,
            Proposal(facts=base_facts(), session_log=fake_credential("aws-access-key")),
            session="s1",
            batch="b1",
            queue=queue,
            expected_fingerprint=UNCHECKED,
        )
    assert store.documents() == []
    assert store.session_logs() == []
    assert queue.pending_sessions()[0].deferrals == 1
