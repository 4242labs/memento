"""Operator controls (ADR D5): view / edit / forget are verbs, not admin scripts."""

from __future__ import annotations

import json

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


def test_recall_on_the_cli_takes_a_budget_and_reports_the_cut(seeded, capsys):
    main(["--store", str(seeded.root), "recall", "lighthouses", "--budget", "3", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["tokens"] <= 3
    assert payload["dropped"] > 0
    assert payload["flags"]


def test_recall_on_the_cli_filters_by_stream(seeded, capsys):
    main(["--store", str(seeded.root), "recall", "suis", "--stream", "errors/fr", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["hits"]
    assert {h["location"] for h in payload["hits"]} == {"errors/fr"}


# ------------------------------------------------------- exit codes are the contract

# An agent branches on these, so "it printed something sensible" is not the assertion that
# matters — the number is. Mutation testing found every one of these unasserted: flipping a
# `return 0` to `return 1` changed nothing any test could see.


def test_the_read_only_verbs_succeed_quietly(seeded, tmp_path, capsys):
    store = str(seeded.root)
    assert main(["--store", store, "status"]) == 0
    assert main(["--store", store, "view"]) == 0
    assert main(["--store", store, "view", "profile.md"]) == 0
    assert main(["--store", store, "facts"]) == 0
    assert main(["--store", store, "history", "profile.md"]) == 0
    assert main(["--store", store, "recall", "kites"]) == 0
    assert main(["--store", store, "recall", "chromodynamics"]) == 0
    assert main(["--store", store, "prompts"]) == 0
    assert main(["--store", store, "prefix", "--adapter", "fixture_consumer:ADAPTER"]) == 0


def test_the_verbs_that_change_something_succeed_quietly(seeded, tmp_path, capsys):
    store = str(seeded.root)
    replacement = tmp_path / "new.md"
    replacement.write_text("## de\n- level: A1\n", encoding="utf-8")

    assert main(["--store", store, "edit", "profile.md", "--from-file", str(replacement)]) == 0
    assert main(["--store", store, "rollback", "profile.md"]) == 0
    assert main(["--store", store, "forget", "languages/de"]) == 0
    assert main(["--store", store, "forget", "languages/fr", "--adapter", "fixture_consumer:ADAPTER"]) == 0


def test_an_unchanged_edit_still_reports_success(seeded, tmp_path, capsys):
    same = tmp_path / "same.md"
    same.write_text(seeded.read_document("profile.md"), encoding="utf-8")
    assert main(["--store", str(seeded.root), "edit", "profile.md", "--from-file", str(same)]) == 0


def test_a_document_with_no_history_is_not_a_failure(seeded, capsys):
    """Nothing to show is an answer, not an error — `history` on a never-replaced document."""
    assert main(["--store", str(seeded.root), "history", "interests.md"]) == 0
    assert main(["--store", str(seeded.root), "history", "nope.md"]) == 0


def test_a_proposal_that_is_not_json_is_a_usage_error_not_a_gate_rejection(seeded, tmp_path, capsys):
    """Exit 2 and exit 3 mean different things: malformed input versus a refused write."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    code = main(["--store", str(seeded.root), "consolidate", "--adapter", "fixture_consumer:ADAPTER",
                 "--proposal", str(bad), "--session", "s9", "--unchecked"])
    assert code == 2
    assert "not valid JSON" in capsys.readouterr().err

    not_an_object = tmp_path / "list.json"
    not_an_object.write_text("[]", encoding="utf-8")
    assert main(["--store", str(seeded.root), "consolidate", "--adapter", "fixture_consumer:ADAPTER",
                 "--proposal", str(not_an_object), "--session", "s9", "--unchecked"]) == 2
