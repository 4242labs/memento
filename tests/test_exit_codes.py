"""The exit-code contract, one row per scenario (B-02 T8 R2/R3).

`docs/agent-consumers.md` tells an agent to branch on these numbers, which makes them the most
load-bearing thing this CLI returns and the cheapest thing to get silently wrong: mutation testing
found sixty places where flipping a `return 0` to a `return 1` changed nothing any test could see.

So every code is pinned here against the scenario that produces it, and the table is the assertion —
adding a verb without adding a row leaves a hole this file is meant not to have.

**In-process, deliberately** (R3). The end-to-end subprocess harness in `test_agent_loop.py` proves
the loop composes across real process boundaries; it is acceptance, and it is far too slow to be
what a mutation run executes per mutant. These call `main()` directly, so a mutant that breaks an
exit code dies in milliseconds.
"""

from __future__ import annotations

import json

import pytest

from memento import cli
from memento.cli import main

SESSION = "260802-150000"

SPEC = {
    "name": "exit-codes",
    "identity_keys": ["id", "topic", "name"],
    "documents": {"operator.md": {"title": "Operator", "sections": ["operator"]}},
    "schema": {"operator.confidence": {"type": "str", "enum": ["low", "medium", "high"]}},
}
FACTS = {"operator": {"confidence": "medium", "timezone": "Europe/Lisbon"}}


@pytest.fixture
def world(tmp_path):
    """A store, a queue, a spec file, and a `run` that goes straight through `main`."""

    class World:
        store = str(tmp_path / "memento")
        queue = str(tmp_path / "queue")
        spec = str(tmp_path / "adapter.json")
        tmp = tmp_path

        as_json = False

        def run(self, *argv: str) -> int:
            extra = ["--json"] if self.as_json else []
            return main(["--store", self.store, *argv, *extra])

        def proposal(self, facts, name="p.json") -> str:
            path = tmp_path / name
            path.write_text(json.dumps({"facts": facts}), encoding="utf-8")
            return str(path)

    (tmp_path / "adapter.json").write_text(json.dumps(SPEC), encoding="utf-8")
    return World()


# --------------------------------------------------------------- the scenarios


def _seed(world) -> None:
    world.run("consolidate", "--adapter-file", world.spec, "--proposal", world.proposal(FACTS),
              "--session", "260802-000001", "--unchecked")


def _ok_status(world):
    return world.run("status")


def _ok_journal(world):
    return world.run("journal", SESSION, "--queue", world.queue, "--text", "material")


def _ok_enqueue(world):
    world.run("journal", SESSION, "--queue", world.queue, "--text", "material")
    return world.run("enqueue", SESSION, "--queue", world.queue)


def _ok_pending(world):
    return world.run("pending", "--queue", world.queue)


def _ok_gate(world):
    return world.run("pending", "--queue", world.queue, "--gate-check",
                     "--idle-seconds", "30", "--prefix-materialized")


def _ok_claim(world):
    return world.run("claim", SESSION)


def _ok_release(world):
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        world.run("claim", "260802-000009")
    printed = buf.getvalue().strip()
    # The token is the payload in `--json` mode and the whole of stdout otherwise; this scenario
    # runs in both, and reading it back is exactly what an agent does.
    token = json.loads(printed)["token"] if printed.startswith("{") else printed
    return world.run("release", "260802-000009", "--token", token)


def _ok_done(world):
    world.run("journal", SESSION, "--queue", world.queue, "--text", "m")
    world.run("enqueue", SESSION, "--queue", world.queue)
    return world.run("done", SESSION, "--queue", world.queue)


def _ok_commit_without_backup(world):
    _seed(world)
    return world.run("commit", "--session", "260802-000001", "--no-push")


def _ok_consolidate(world):
    return world.run("consolidate", "--adapter-file", world.spec, "--proposal", world.proposal(FACTS),
                     "--session", SESSION, "--unchecked")


def _ok_prefix(world):
    _seed(world)
    return world.run("prefix", "--adapter-file", world.spec)


def _ok_facts(world):
    _seed(world)
    return world.run("facts")


def _ok_fingerprint(world):
    _seed(world)
    return world.run("facts", "--fingerprint")


def _ok_recall(world):
    _seed(world)
    return world.run("recall", "Lisbon")


def _ok_recall_no_match(world):
    _seed(world)
    return world.run("recall", "chromodynamics")


def _ok_view(world):
    _seed(world)
    return world.run("view", "operator.md")


def _ok_view_list(world):
    _seed(world)
    return world.run("view")


def _ok_history(world):
    _seed(world)
    return world.run("history", "operator.md")


def _ok_history_absent(world):
    return world.run("history", "never-written.md")


def _ok_edit(world):
    _seed(world)
    new = world.tmp / "new.md"
    new.write_text("# Operator\n\n## operator\n- **confidence**: low\n", encoding="utf-8")
    return world.run("edit", "operator.md", "--from-file", str(new))


def _ok_edit_unchanged(world):
    _seed(world)
    same = world.tmp / "same.md"
    from memento import MemoryStore

    same.write_text(MemoryStore(world.store).read_document("operator.md") or "", encoding="utf-8")
    return world.run("edit", "operator.md", "--from-file", str(same))


def _ok_rollback(world):
    _ok_edit(world)
    return world.run("rollback", "operator.md")


def _ok_forget(world):
    _seed(world)
    return world.run("forget", "operator/timezone")


def _ok_backup(world):
    return world.run("backup", "--yes")


def _ok_prompts(world):
    return world.run("prompts")


def _usage_push_failed(world):
    """Backup enabled, remote unreachable. The write landed; the copy did not, and that is a FLAG.

    Left untested this branch returned success: mutation testing found the only two survivors that
    could still change an exit code, both here.
    """
    _seed(world)
    world.run("backup", "--yes", "--remote", str(world.tmp / "nowhere.git"))
    return world.run("commit", "--session", "260802-000001")


def _usage_missing_adapter(world):
    return world.run("prefix")


def _usage_missing_document(world):
    return world.run("view", "nope.md")


def _usage_journal_without_text(world):
    return world.run("journal", SESSION, "--queue", world.queue)


def _usage_done_never_enqueued(world):
    return world.run("done", "260802-999999", "--queue", world.queue)


def _usage_backup_without_yes(world):
    return world.run("backup", "--remote", "git@example.com:x/y.git")


def _usage_from_store_without_adapter(world):
    return world.run("facts", "--from-store")


def _malformed_proposal(world):
    bad = world.tmp / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    return world.run("consolidate", "--adapter-file", world.spec, "--proposal", str(bad),
                     "--session", SESSION, "--unchecked")


def _malformed_proposal_not_an_object(world):
    listed = world.tmp / "list.json"
    listed.write_text("[]", encoding="utf-8")
    return world.run("consolidate", "--adapter-file", world.spec, "--proposal", str(listed),
                     "--session", SESSION, "--unchecked")


def _gate_rejected(world):
    _seed(world)
    eroded = {"operator": {"confidence": "medium"}}  # drops `timezone` without a tombstone
    return world.run("consolidate", "--adapter-file", world.spec, "--proposal",
                     world.proposal(eroded, "eroded.json"), "--session", SESSION, "--unchecked")


def _secrets(world):
    from support.fake_credentials import fake_credential

    poisoned = json.loads(json.dumps(FACTS))
    poisoned["operator"]["note"] = fake_credential("aws-access-key")
    return world.run("consolidate", "--adapter-file", world.spec, "--proposal",
                     world.proposal(poisoned, "poisoned.json"), "--session", SESSION, "--unchecked")


def _stale(world):
    from memento.writepath import facts_fingerprint

    _seed(world)
    grown = json.loads(json.dumps(FACTS))
    grown["operator"]["extra"] = "yes"
    return world.run("consolidate", "--adapter-file", world.spec,
                     "--proposal", world.proposal(grown, "grown.json"),
                     "--session", SESSION, "--expect", facts_fingerprint({}))


def _claim_held(world):
    world.run("claim", SESSION)
    return world.run("claim", SESSION)


def _release_wrong_token(world):
    world.run("claim", SESSION)
    return world.run("release", SESSION, "--token", "not-the-token")


def _drain_refused_idle(world):
    return world.run("pending", "--queue", world.queue, "--gate-check",
                     "--idle-seconds", "1", "--prefix-materialized")


def _drain_refused_prefix(world):
    return world.run("pending", "--queue", world.queue, "--gate-check", "--idle-seconds", "30")


#: scenario → the exit code the contract promises. Adding a verb means adding a row.
CONTRACT = [
    ("status", _ok_status, cli.EXIT_OK),
    ("journal", _ok_journal, cli.EXIT_OK),
    ("enqueue", _ok_enqueue, cli.EXIT_OK),
    ("pending", _ok_pending, cli.EXIT_OK),
    ("pending --gate-check passes", _ok_gate, cli.EXIT_OK),
    ("claim", _ok_claim, cli.EXIT_OK),
    ("release", _ok_release, cli.EXIT_OK),
    ("done", _ok_done, cli.EXIT_OK),
    ("commit on a store with no backup", _ok_commit_without_backup, cli.EXIT_OK),
    ("consolidate", _ok_consolidate, cli.EXIT_OK),
    ("prefix", _ok_prefix, cli.EXIT_OK),
    ("facts", _ok_facts, cli.EXIT_OK),
    ("facts --fingerprint", _ok_fingerprint, cli.EXIT_OK),
    ("recall with hits", _ok_recall, cli.EXIT_OK),
    ("recall with no hits", _ok_recall_no_match, cli.EXIT_OK),
    ("view a document", _ok_view, cli.EXIT_OK),
    ("view the list", _ok_view_list, cli.EXIT_OK),
    ("history", _ok_history, cli.EXIT_OK),
    ("history of a never-written document", _ok_history_absent, cli.EXIT_OK),
    ("edit", _ok_edit, cli.EXIT_OK),
    ("edit that changes nothing", _ok_edit_unchanged, cli.EXIT_OK),
    ("rollback", _ok_rollback, cli.EXIT_OK),
    ("forget", _ok_forget, cli.EXIT_OK),
    ("backup --yes", _ok_backup, cli.EXIT_OK),
    ("prompts", _ok_prompts, cli.EXIT_OK),

    ("a verb that needs an adapter and has none", _usage_missing_adapter, cli.EXIT_USAGE),
    ("view of a missing document", _usage_missing_document, cli.EXIT_USAGE),
    ("journal with neither --text nor --show", _usage_journal_without_text, cli.EXIT_USAGE),
    ("done on a session never enqueued", _usage_done_never_enqueued, cli.EXIT_USAGE),
    ("backup without --yes", _usage_backup_without_yes, cli.EXIT_USAGE),
    ("--from-store with no adapter", _usage_from_store_without_adapter, cli.EXIT_USAGE),
    ("a backup push that fails", _usage_push_failed, cli.EXIT_USAGE),

    ("a proposal that is not JSON", _malformed_proposal, cli.EXIT_MALFORMED),
    ("a proposal that is not an object", _malformed_proposal_not_an_object, cli.EXIT_MALFORMED),

    ("a proposal the gates reject", _gate_rejected, cli.EXIT_GATE_REJECTED),
    ("a proposal carrying a credential", _secrets, cli.EXIT_SECRETS),
    ("a proposal derived from a state that moved", _stale, cli.EXIT_STALE),

    ("claiming a session someone else holds", _claim_held, cli.EXIT_CLAIM_HELD),
    ("releasing under the wrong token", _release_wrong_token, cli.EXIT_CLAIM_HELD),

    ("the gate refusing on idle", _drain_refused_idle, cli.EXIT_DRAIN_REFUSED),
    ("the gate refusing on an unmaterialized prefix", _drain_refused_prefix, cli.EXIT_DRAIN_REFUSED),
]


@pytest.mark.parametrize("name,scenario,expected", CONTRACT, ids=[row[0] for row in CONTRACT])
def test_the_exit_code_contract(world, name, scenario, expected, capsys):
    assert scenario(world) == expected, f"{name} did not exit {expected}"


def test_every_documented_code_has_at_least_one_scenario():
    """A code nothing produces is a promise nothing keeps."""
    promised = {
        cli.EXIT_OK, cli.EXIT_USAGE, cli.EXIT_MALFORMED, cli.EXIT_GATE_REJECTED,
        cli.EXIT_SECRETS, cli.EXIT_STALE, cli.EXIT_CLAIM_HELD, cli.EXIT_DRAIN_REFUSED,
    }
    assert {row[2] for row in CONTRACT} == promised


@pytest.mark.parametrize("name,scenario,expected", CONTRACT, ids=[row[0] for row in CONTRACT])
def test_every_verb_answers_the_same_way_in_json(world, name, scenario, expected, capsys):
    """`--json` is the parse surface, so it must carry the same verdict the exit code does.

    Both halves of the contract, checked against each other on every scenario: an `ok: true` beside
    a non-zero exit would send a parsing consumer down the success path of a failed write.
    """
    world.as_json = True
    code = scenario(world)
    payloads = _payloads(capsys.readouterr().out)

    assert code == expected, name
    assert payloads, f"{name} printed no JSON payload"
    assert payloads[-1]["ok"] is (expected == cli.EXIT_OK), name


def _payloads(text: str) -> list[dict]:
    """Every JSON object printed. A scenario may drive several commands; the last is its verdict."""
    decoder, out, index = json.JSONDecoder(), [], 0
    while index < len(text):
        if text[index] in " \n\t\r":
            index += 1
            continue
        obj, index = decoder.raw_decode(text, index)
        out.append(obj)
    return out


# ------------------------------------------------- nothing escapes as a traceback

# An uncaught exception hands an agent a stack trace and the interpreter's own exit code, which
# makes "branch on the exit code" a promise this CLI does not keep. Both of these arrived that way.


def test_a_missing_adapter_file_is_an_exit_code_not_a_traceback(world):
    assert world.run("prefix", "--adapter-file", str(world.tmp / "not-here.json")) == cli.EXIT_USAGE


def test_a_required_prefix_section_that_cannot_fit_is_an_exit_code(world, tmp_path):
    """`BudgetError` is a refusal to ship a degraded prefix, not a crash."""
    tight = tmp_path / "tight.json"
    tight.write_text(
        json.dumps({
            **SPEC,
            "prefix_budget_tokens": 1,
            "prefix_sections": [
                {"name": "operator", "priority": 0, "document": "operator.md", "required": True}
            ],
        }),
        encoding="utf-8",
    )
    world.run("consolidate", "--adapter-file", world.spec, "--proposal", world.proposal(FACTS),
              "--session", "260802-000001", "--unchecked")

    assert world.run("prefix", "--adapter-file", str(tight)) == cli.EXIT_USAGE


def test_a_credential_still_raises_loudly_rather_than_becoming_an_exit_code(world, tmp_path):
    """The one exception the handler names and refuses to swallow.

    A credential reaching the store is not a status to report and move past. The blanket handler
    this file's siblings asked for would have turned it into a quiet exit code, which is exactly the
    regression `test_the_cli_edit_verb_is_gated` exists to hold.
    """
    from memento.errors import SecretsDetected
    from support.fake_credentials import fake_credential

    paste = tmp_path / "paste.md"
    paste.write_text(fake_credential("aws-access-key") + "\n", encoding="utf-8")

    with pytest.raises(SecretsDetected):
        world.run("edit", "notes.md", "--from-file", str(paste))


def test_the_fingerprint_is_the_one_the_write_path_compares_against(world):
    """`--from-store` answers a different question, and must not get to answer this one.

    A fact no document projects survives the round-trip check — an unprojected key renders to
    nothing either way — but is absent from the parse. Taking the token from that parse hands back
    a fingerprint no consolidation can ever match, and the agent redrives forever.
    """
    import contextlib
    import io

    from memento import MemoryStore, adapter_from_spec
    from memento.writepath import facts_fingerprint, read_facts

    unprojected = json.loads(json.dumps(FACTS))
    unprojected["internal"] = {"seen": "3"}  # no declared document covers `internal`
    world.run("consolidate", "--adapter-file", world.spec,
              "--proposal", world.proposal(unprojected, "unprojected.json"),
              "--session", "260802-000001", "--unchecked")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert world.run("facts", "--fingerprint", "--adapter-file", world.spec, "--from-store") == 0
    printed = buf.getvalue().strip()

    store = MemoryStore(world.store)
    adapter = adapter_from_spec(SPEC)
    assert printed == facts_fingerprint(read_facts(store, adapter))
