"""Projected documents: `document_replaced` history, rollback, and the append↔replace crash window.

The LLM-authored documents are not reconstructible from the event log, so every replace appends an
event carrying the prior content. Without it, one bad consolidation that passed the gates would
destroy the prior profile irrecoverably on a git-less store.
"""

from __future__ import annotations

import pytest

from memento import DocumentWrite, MementoError, MemoryStore
from memento.errors import CorruptStoreError
from memento.forgetting import document_revisions, rollback_document
from memento.store import OBJECTS_DIR


def test_replace_records_the_prior_content(store):
    store.replace_document("profile.md", "v1\n", session="s1", batch="b1")
    store.replace_document("profile.md", "v2\n", session="s2", batch="b2")

    history = store.document_history("profile.md")
    assert [e.payload["prior_content"] for e in history] == [None, "v1\n"]
    assert store.read_document("profile.md") == "v2\n"


def test_rollback_restores_and_is_itself_recorded(store):
    store.replace_document("profile.md", "v1\n", session="s1", batch="b1")
    store.replace_document("profile.md", "v2\n", session="s2", batch="b2")

    rollback_document(store, "profile.md", session="s3", batch="b3")

    assert store.read_document("profile.md") == "v1\n"
    assert len(store.document_history("profile.md")) == 3  # the rollback is in the history too


def test_re_running_a_landed_replace_appends_no_duplicate(store):
    """The `(session, batch, document, ordinal)` key. A second run must not capture v2 as 'prior'."""
    store.replace_document("profile.md", "v1\n", session="s1", batch="b1")
    store.replace_document("profile.md", "v2\n", session="s2", batch="b2")

    result = store.replace_document("profile.md", "v2\n", session="s2", batch="b2")

    assert result.replayed
    history = store.document_history("profile.md")
    assert len(history) == 2
    assert history[-1].payload["prior_content"] == "v1\n"


def test_crash_between_append_and_replace_is_completed_on_re_run(store, monkeypatch):
    """Kill the process after the event lands but before the file is swapped."""
    store.replace_document("profile.md", "v1\n", session="s1", batch="b1")

    import memento.store as store_mod

    def die(*args, **kwargs):
        raise RuntimeError("power cut")

    monkeypatch.setattr(store_mod, "_atomic_write_text", die)
    with pytest.raises(RuntimeError):
        store.replace_document("profile.md", "v2\n", session="s2", batch="b2")

    # Mid-window: the event is recorded, the file still holds v1. Nothing is lost either way.
    assert store.read_document("profile.md") == "v1\n"
    assert len(store.document_history("profile.md")) == 2

    monkeypatch.undo()
    result = store.replace_document("profile.md", "v2\n", session="s2", batch="b2")

    assert result.replayed
    assert store.read_document("profile.md") == "v2\n"
    assert len(store.document_history("profile.md")) == 2  # still no duplicate


def test_the_same_batch_with_different_content_is_a_new_revision_not_a_conflict(store):
    """A redrive re-runs the model, whose output differs. That has to be able to land.

    Refusing it would leave the session permanently unconsolidatable — the crash it was recovering
    from would become permanent.
    """
    store.replace_document("profile.md", "v1\n", session="s1", batch="b1")
    result = store.replace_document("profile.md", "v2 from the redrive\n", session="s1", batch="b1")

    assert not result.replayed
    assert store.read_document("profile.md") == "v2 from the redrive\n"
    history = store.document_history("profile.md")
    assert [e.payload["prior_content"] for e in history] == [None, "v1\n"]


def test_multiple_documents_share_one_batch(store):
    store.replace_documents(
        [DocumentWrite("profile.md", "p\n"), DocumentWrite("interests.md", "i\n")],
        session="s1",
        batch="b1",
    )
    assert store.read_document("profile.md") == "p\n"
    assert store.read_document("interests.md") == "i\n"
    assert len(store.document_log().read()) == 2


def test_the_same_document_twice_in_one_batch_is_refused(store):
    with pytest.raises(MementoError, match="same document twice"):
        store.replace_documents(
            [DocumentWrite("profile.md", "a\n"), DocumentWrite("profile.md", "b\n")],
            session="s1",
            batch="b1",
        )


def test_large_prior_content_goes_to_the_retained_object_area(tmp_path, clock):
    """The hash+pointer variant must point into a retained area, or rollback is unavailable."""
    store = MemoryStore(tmp_path / "memory", clock=clock, inline_limit=64)
    big = "x" * 500 + "\n"
    store.replace_document("profile.md", big, session="s1", batch="b1")
    store.replace_document("profile.md", "small\n", session="s2", batch="b2")

    event = store.document_history("profile.md")[-1]
    assert "prior_content" not in event.payload
    ref = event.payload["prior_ref"]
    assert (store.root / OBJECTS_DIR / ref).exists()

    assert store.prior_content(event) == big
    rollback_document(store, "profile.md", session="s3", batch="b3")
    assert store.read_document("profile.md") == big


def test_a_missing_object_reports_rollback_unavailable_rather_than_lying(tmp_path, clock):
    store = MemoryStore(tmp_path / "memory", clock=clock, inline_limit=64)
    store.replace_document("profile.md", "y" * 500 + "\n", session="s1", batch="b1")
    store.replace_document("profile.md", "small\n", session="s2", batch="b2")

    event = store.document_history("profile.md")[-1]
    (store.root / OBJECTS_DIR / event.payload["prior_ref"]).unlink()

    with pytest.raises(CorruptStoreError, match="rollback is unavailable"):
        store.prior_content(event)


def test_revisions_report_whether_rollback_is_available(store):
    store.replace_document("profile.md", "v1\n", session="s1", batch="b1")
    store.replace_document("profile.md", "v2\n", session="s2", batch="b2")

    revisions = document_revisions(store, "profile.md")
    assert [r.has_prior for r in revisions] == [False, True]

    with pytest.raises(MementoError, match="rollback is unavailable"):
        rollback_document(store, "profile.md", session="s3", batch="b3", revision=0)


def test_a_reader_never_sees_a_half_written_document(store):
    """Wholesale replacement is atomic: the temp file is swapped in, never written in place."""
    store.replace_document("profile.md", "v1\n", session="s1", batch="b1")
    inode_before = store.document_path("profile.md").stat().st_ino
    store.replace_document("profile.md", "v2\n", session="s2", batch="b2")
    assert store.document_path("profile.md").stat().st_ino != inode_before
