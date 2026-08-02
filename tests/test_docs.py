"""The documented CLI is the CLI (B-02 AC-7).

`docs/agent-consumers.md` is not prose about the engine — for a consumer with no Python it *is* the
interface definition. An agent copies those invocations verbatim. A flag that gets renamed leaves the
doc describing a command that does not exist, and the reader finds out at 2am with a session's
material sitting unconsolidated in a queue.

So every `memento ...` line in the docs is parsed by the real parser here. Shell variables are
substituted with plausible values; nothing is executed.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from memento.cli import build_parser

DOCS = sorted((Path(__file__).resolve().parents[1] / "docs").glob("*.md"))

#: Values for the shell variables the documented examples use. Substituted, not executed.
SUBSTITUTIONS = {
    "$SESSION": "260802-140000",
    "$IDLE": "30",
    "$FP": "0" * 64,
    '"$IDLE"': "30",
    '"$FP"': "0" * 64,
    '"$TOKEN"': "deadbeefdeadbeef",
}


def _invocations(text: str) -> list[str]:
    """Every `memento ...` command in a fenced block, joined across backslash continuations."""
    joined = re.sub(r"\\\n\s*", " ", text)
    out = []
    for raw in joined.splitlines():
        # Comment first, then command substitution: `VAR=$(memento ...)   # note` leaves a stray
        # `)` on the command otherwise, and argparse happily swallows it as a positional value —
        # so the check passes while testing a session id that could never exist.
        line = raw.split("#")[0].strip()
        capture = re.match(r"^[A-Z_]+=\$\((?P<inner>.+)\)$", line)
        if capture:
            line = capture.group("inner")
        if not line.startswith("memento "):
            continue
        command = line.strip()
        if " ... " in f" {command} ":
            continue  # a deliberately elided example in prose, not a copyable invocation
        out.append(command)
    return out


def _argv(command: str) -> list[str]:
    for token, value in SUBSTITUTIONS.items():
        command = command.replace(token, value)
    return shlex.split(command)[1:]  # drop the program name


def test_the_docs_contain_invocations_to_check():
    """A silently empty corpus would make every assertion below vacuously true."""
    found = sum(len(_invocations(doc.read_text(encoding="utf-8"))) for doc in DOCS)
    assert found >= 15, f"only {found} documented invocations found; the extractor has drifted"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_documented_invocation_parses(doc):
    parser = build_parser()
    for command in _invocations(doc.read_text(encoding="utf-8")):
        try:
            parser.parse_args(_argv(command))
        except SystemExit as exc:  # argparse exits rather than raising on a bad flag
            raise AssertionError(f"{doc.name} documents a command the CLI rejects:\n  {command}") from exc


def test_every_documented_exit_code_is_one_the_cli_can_return():
    """The exit-code table is the contract an agent branches on; drift there is silent."""
    from memento import cli

    documented = {
        int(m)
        for m in re.findall(
            r"^\| `(\d)` \|",
            (Path(__file__).resolve().parents[1] / "docs" / "agent-consumers.md").read_text(
                encoding="utf-8"
            ),
            flags=re.MULTILINE,
        )
    }
    emitted = {
        cli.EXIT_GATE_REJECTED,
        cli.EXIT_SECRETS,
        cli.EXIT_STALE,
        cli.EXIT_CLAIM_HELD,
        cli.EXIT_DRAIN_REFUSED,
    }

    assert emitted <= documented, f"undocumented exit codes: {sorted(emitted - documented)}"
    assert documented - emitted == {0, 1}, "the table documents a code nothing returns"
