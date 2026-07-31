"""Operator controls (ADR D5): view / edit / forget are verbs, not admin scripts."""

from __future__ import annotations

from memento.cli import main
from memento.writepath import read_facts, read_tombstones


def test_status_reports_what_the_store_holds(seeded, queue, capsys):
    queue.close_and_enqueue("s2")
    code = main(["--store", str(seeded.root), "status", "--queue", str(queue.root)])
    out = capsys.readouterr().out

    assert code == 0
    assert "profile.md" in out and "errors/fr" in out
    assert "backup:         off" in out
    assert "pending:        1" in out


def test_view_prints_a_document_and_lists_them(seeded, capsys):
    main(["--store", str(seeded.root), "view"])
    assert "profile.md" in capsys.readouterr().out

    main(["--store", str(seeded.root), "view", "profile.md"])
    assert "## de" in capsys.readouterr().out


def test_view_of_a_missing_document_fails_loudly(seeded, capsys):
    assert main(["--store", str(seeded.root), "view", "nope.md"]) == 1


def test_facts_prints_the_structured_state(seeded, capsys):
    main(["--store", str(seeded.root), "facts"])
    assert '"level": "B1"' in capsys.readouterr().out


def test_edit_keeps_the_prior_content(seeded, tmp_path, capsys):
    replacement = tmp_path / "new.md"
    replacement.write_text("## de\n- level: A1\n", encoding="utf-8")

    main(["--store", str(seeded.root), "edit", "profile.md", "--from-file", str(replacement)])

    assert seeded.read_document("profile.md") == "## de\n- level: A1\n"
    assert seeded.document_history("profile.md")[-1].payload["prior_content"].startswith("## de")


def test_edit_with_no_change_writes_nothing(seeded, tmp_path, capsys):
    same = tmp_path / "same.md"
    same.write_text(seeded.read_document("profile.md"), encoding="utf-8")
    before = len(seeded.document_history("profile.md"))

    main(["--store", str(seeded.root), "edit", "profile.md", "--from-file", str(same)])

    assert "unchanged" in capsys.readouterr().out
    assert len(seeded.document_history("profile.md")) == before


def test_history_and_rollback(seeded, tmp_path, capsys):
    original = seeded.read_document("profile.md")
    replacement = tmp_path / "new.md"
    replacement.write_text("## de\n- level: A1\n", encoding="utf-8")
    main(["--store", str(seeded.root), "edit", "profile.md", "--from-file", str(replacement)])

    main(["--store", str(seeded.root), "history", "profile.md"])
    assert "session=" in capsys.readouterr().out

    main(["--store", str(seeded.root), "rollback", "profile.md"])
    assert seeded.read_document("profile.md") == original


def test_forget_without_an_adapter_tombstones_and_says_what_it_did_not_do(seeded, capsys):
    main(["--store", str(seeded.root), "forget", "languages/de"])
    out = capsys.readouterr().out

    assert "languages/de" in read_tombstones(seeded)
    assert "not re-rendered" in out
    assert "de" in read_facts(seeded)["languages"]  # documents untouched, and it says so


def test_forget_with_an_adapter_re_renders(seeded, capsys):
    main(
        [
            "--store",
            str(seeded.root),
            "forget",
            "languages/de",
            "--adapter",
            "fixture_consumer:ADAPTER",
        ]
    )
    assert "de" not in read_facts(seeded)["languages"]


def test_recall_finds_and_reports_nothing_when_there_is_nothing(seeded, capsys):
    main(["--store", str(seeded.root), "recall", "lighthouses"])
    assert "lighthouses" in capsys.readouterr().out

    main(["--store", str(seeded.root), "recall", "chromodynamics"])
    assert "no matches" in capsys.readouterr().out


def test_backup_refuses_without_the_acknowledgement(seeded, capsys):
    code = main(["--store", str(seeded.root), "backup", "--remote", "git@example.com:x/y.git"])
    assert code == 1
    assert "PRIVATE remote" in capsys.readouterr().err
    assert not (seeded.root / ".git").exists()


def test_backup_enables_with_the_acknowledgement(seeded, capsys):
    assert main(["--store", str(seeded.root), "backup", "--yes"]) == 0
    assert (seeded.root / ".git").exists()


def test_prompts_prints_the_pinned_templates(seeded, capsys):
    main(["--store", str(seeded.root), "prompts"])
    assert "Reference the past sparingly" in capsys.readouterr().out
