"""Secrets never enter the store (ADR D8) — a write gate, not an afterthought."""

from __future__ import annotations

import pytest

from conftest import base_facts
from memento.writepath import UNCHECKED
from memento import Proposal, SecretsDetected, apply_consolidation
from memento.flags import SECRETS_REJECTED, FlagSink
from memento.secrets import scan

SAMPLES = {
    "aws-access-key": "AKIA" + "IOSFODNN7EXAMPLE",
    "github-token": "ghp_" + "a" * 36,
    "anthropic-key": "sk-ant-" + "b" * 40,
    "private-key-block": "-----BEGIN RSA PRIVATE " + "KEY-----",
    "assigned-secret": "api_key" + ' = "0123456789abcdefghij"',
}


@pytest.mark.parametrize("kind,sample", sorted(SAMPLES.items()))
def test_each_pattern_is_detected(kind, sample):
    assert kind in {m.pattern for m in scan(sample)}


def test_ordinary_prose_is_not_a_secret():
    assert scan("The learner said they work in software and enjoy films.") == []


def test_a_secret_in_a_document_is_refused_and_nothing_is_written(seeded, adapter, queue):
    before = seeded.read_document("profile.md")
    sink = FlagSink()
    proposal = Proposal(
        facts=base_facts(),
        documents={"profile.md": "## en\n- token: ghp_" + "c" * 36 + "\n"},
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
        entries={"vocab/en": [{"id": "v1", "item": "AKIA" + "IOSFODNN7EXAMPLE"}]},
    )
    with pytest.raises(SecretsDetected):
        apply_consolidation(seeded, adapter, proposal, session="s2", batch="b2", expected_fingerprint=UNCHECKED)


def test_a_secret_in_the_session_log_is_refused(seeded, adapter):
    proposal = Proposal(
        facts=base_facts(),
        session_log="they pasted -----BEGIN OPENSSH PRIVATE " + "KEY----- into the chat",
    )
    with pytest.raises(SecretsDetected):
        apply_consolidation(seeded, adapter, proposal, session="s2", batch="b2", expected_fingerprint=UNCHECKED)


def test_a_secret_in_the_facts_is_refused(seeded, adapter):
    facts = base_facts()
    facts["languages"]["fr"]["goals"] = "remember my token ghp_" + "d" * 36
    with pytest.raises(SecretsDetected):
        apply_consolidation(seeded, adapter, Proposal(facts=facts), session="s2", batch="b2", expected_fingerprint=UNCHECKED)


def test_the_gate_fires_before_anything_touches_disk(store, adapter, queue):
    """Rejection order matters: a secret must not reach the store even transiently."""
    queue.close_and_enqueue("s1")
    with pytest.raises(SecretsDetected):
        apply_consolidation(
            store,
            adapter,
            Proposal(facts=base_facts(), session_log="AKIA" + "IOSFODNN7EXAMPLE"),
            session="s1",
            batch="b1",
            queue=queue,
            expected_fingerprint=UNCHECKED,
        )
    assert store.documents() == []
    assert store.session_logs() == []
    assert queue.pending_sessions()[0].deferrals == 1
