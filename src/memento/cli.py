"""The command-line surface: argument parsing, dispatch, and the choice of output mode.

Memory the operator cannot inspect and correct is memory they have to trust blindly, so `view`,
`edit` and `forget` are part of the engine rather than something each consumer reinvents. Every
write goes through the same event log and the same gates as a consolidation — `forget` is honored
*because* it writes a tombstone, not because the CLI is privileged.

The second consumer class is an **agent**: markdown and a shell, no import statement anywhere. For
it this CLI *is* the API, so it gets the session lifecycle too — `journal`, `enqueue`, `pending`,
`claim`/`release`, `done`, `commit`. What it does not get is a weaker engine: the gates, the
compare-and-swap and the drain gate all apply through the shell exactly as they apply to a library
caller. See `docs/agent-consumers.md`.

**Two things here are contractual and one is not.** The exit code and the `--json` payload are
promises. Console prose is not — it lives in `presentation.py`, is free to be reworded, and a
consumer parsing it is relying on something this project does not promise. What each verb *does*
lives in `commands.py`; this module only wires arguments to it and picks which of the two output
modes to use.

Exit codes — an agent branches on these:

    0  fine        3  gates rejected it     5  the store moved underneath it (redrive)
    1  usage/IO    4  secrets                6  another claimant holds the session
    2  malformed input                       7  the drain gate refuses: not yet
"""

from __future__ import annotations

import argparse
import json
import sys

from . import presentation
from .commands import (
    Outcome,
    EXIT_CLAIM_HELD,
    EXIT_DRAIN_REFUSED,
    EXIT_GATE_REJECTED,
    EXIT_MALFORMED,
    EXIT_OK,
    EXIT_SECRETS,
    EXIT_STALE,
    EXIT_USAGE,
    cmd_backup,
    cmd_claim,
    cmd_commit,
    cmd_consolidate,
    cmd_done,
    cmd_edit,
    cmd_enqueue,
    cmd_facts,
    cmd_forget,
    cmd_history,
    cmd_journal,
    cmd_pending,
    cmd_prefix,
    cmd_prompts,
    cmd_recall,
    cmd_release,
    cmd_rollback,
    cmd_status,
    cmd_view,
)
from .errors import MementoError, SecretsDetected
from .locking import DEFAULT_CLAIM_TTL

__all__ = [
    "EXIT_CLAIM_HELD",
    "EXIT_DRAIN_REFUSED",
    "EXIT_GATE_REJECTED",
    "EXIT_MALFORMED",
    "EXIT_OK",
    "EXIT_SECRETS",
    "EXIT_STALE",
    "EXIT_USAGE",
    "build_parser",
    "main",
]


def _adapter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--adapter", default=None, help="module:attribute")
    parser.add_argument("--adapter-file", default=None, help="path to a declared adapter spec (JSON)")


def _json_arg(parser: argparse.ArgumentParser) -> None:
    """The parse surface. Every verb an agent drives has it; console prose is not contractual."""
    parser.add_argument("--json", action="store_true", help="print the machine-readable result")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memento", description=__doc__)
    parser.add_argument("--store", required=True, help="path to the store root")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="what this store holds right now")
    p.add_argument("--queue", default=None, help="also report the queue's pending sessions")
    _json_arg(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("view", help="print a projected document, or list them")
    p.add_argument("document", nargs="?", help="the document to print; omit to list them")
    _json_arg(p)
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("facts", help="print the structured facts behind the documents")
    p.add_argument("--fingerprint", action="store_true", help="print only the compare-and-swap token")
    p.add_argument(
        "--from-store",
        action="store_true",
        help="parse the projected documents back into facts; refuses if the bytes do not round-trip",
    )
    _adapter_args(p)
    _json_arg(p)
    p.set_defaults(func=cmd_facts)

    p = sub.add_parser("prefix", help="assemble the budgeted core prefix for a session")
    p.add_argument("--budget", type=int, default=None, help="token ceiling; the adapter's if unset")
    _adapter_args(p)
    _json_arg(p)
    p.set_defaults(func=cmd_prefix)

    p = sub.add_parser("consolidate", help="submit a proposal through the write gates")
    p.add_argument("--proposal", required=True, help="path to a proposal JSON, or - for stdin")
    p.add_argument("--session", required=True, help="the session this consolidation is for")
    p.add_argument("--batch", default="consolidate", help="batch id for idempotent replay")
    p.add_argument("--queue", default=None, help="mark the session deferred if this is rejected")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--expect", help="fingerprint from `facts --fingerprint`")
    group.add_argument(
        "--unchecked",
        action="store_true",
        help="opt out of the compare-and-swap, deliberately (first write to an empty store)",
    )
    _adapter_args(p)
    _json_arg(p)
    p.set_defaults(func=cmd_consolidate)

    p = sub.add_parser("journal", help="append this turn's material, or print what accumulated")
    p.add_argument("session", help="the session this material belongs to")
    p.add_argument("--queue", required=True, help="path to the queue root")
    p.add_argument("--turn", type=int, default=0, help="turn ordinal within the session")
    p.add_argument("--text", default=None, help="the turn's material, or - for stdin")
    p.add_argument("--show", action="store_true", help="print the journal instead of appending")
    _json_arg(p)
    p.set_defaults(func=cmd_journal)

    p = sub.add_parser("enqueue", help="session exit: close the journal and mark it pending")
    p.add_argument("session", help="the session that just ended")
    p.add_argument("--queue", required=True, help="path to the queue root")
    _json_arg(p)
    p.set_defaults(func=cmd_enqueue)

    p = sub.add_parser("pending", help="what is waiting, and whether consolidation may start")
    p.add_argument("--queue", required=True, help="path to the queue root")
    p.add_argument(
        "--gate-check",
        action="store_true",
        help="refuse (exit 7) unless the drain gate's preconditions hold",
    )
    p.add_argument("--idle-seconds", type=float, default=0.0, help="how long the session has been idle")
    p.add_argument("--min-idle-seconds", type=float, default=5.0, help="the idle bar to clear")
    p.add_argument(
        "--prefix-materialized", action="store_true", help="the read prefix is fully assembled"
    )
    _json_arg(p)
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("done", help="write the consolidated marker — last, after commit")
    p.add_argument("session", help="the session that was consolidated")
    p.add_argument("--queue", required=True, help="path to the queue root")
    _adapter_args(p)  # only for its retention policy: pruning is gated on the marker this writes
    _json_arg(p)
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("claim", help="claim a session's consolidation; prints the release token")
    p.add_argument("session", help="the session to claim")
    p.add_argument("--ttl", type=float, default=DEFAULT_CLAIM_TTL, help="seconds before it goes stale")
    _json_arg(p)
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("release", help="give a claimed session back")
    p.add_argument("session", help="the session to release")
    p.add_argument("--token", required=True, help="the token `claim` printed")
    _json_arg(p)
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("commit", help="commit and push a consolidation on a backup-enabled store")
    p.add_argument("--session", required=True, help="the consolidated session, for attribution")
    p.add_argument("--no-push", dest="push", action="store_false", default=True, help="commit only")
    _json_arg(p)
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("history", help="revisions of a document")
    p.add_argument("document", help="the document to show revisions for")
    _json_arg(p)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("rollback", help="restore a document's prior content")
    p.add_argument("document", help="the document to roll back")
    p.add_argument("--revision", type=int, default=-1, help="which revision; -1 is the latest prior")
    _json_arg(p)
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("edit", help="edit a document; the prior content is kept")
    p.add_argument("document", help="the document to edit")
    p.add_argument("--from-file", default=None, help="take the new content from here instead of $EDITOR")
    _json_arg(p)
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("forget", help="tombstone a fact; never a delete")
    p.add_argument("marker", help="path/key as reported by the gates, e.g. languages/de")
    p.add_argument("--reason", default="operator forget", help="why, for the audit trail")
    _adapter_args(p)
    _json_arg(p)
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("recall", help="search events and documents")
    p.add_argument("query", help="terms to search for; a hit must share one")
    p.add_argument("--limit", type=int, default=10, help="maximum number of hits")
    p.add_argument("--budget", type=int, default=None, help="token ceiling; adapter's if unset")
    p.add_argument("--stream", action="append", default=[], help="restrict to a stream; repeatable")
    p.add_argument("--key", action="append", default=[], help="restrict to an entry id; repeatable")
    p.add_argument("--since", default=None, help="ISO-8601 lower bound on an entry's last-seen time")
    p.add_argument("--until", default=None, help="ISO-8601 upper bound")
    _adapter_args(p)
    _json_arg(p)
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("backup", help="opt this store into git backup")
    p.add_argument("--remote", default=None, help="the private remote to push to")
    p.add_argument("--branch", default="main", help="branch to track")
    p.add_argument("--yes", action="store_true", help="acknowledge the warning")
    _json_arg(p)
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("prompts", help="print the engine's pinned relationship/restraint templates")
    _json_arg(p)
    p.set_defaults(func=cmd_prompts)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        outcome = args.func(args)
    except SecretsDetected:
        # The one error that must stay loud. A credential reaching the store is not a status to
        # report and move past, and `test_the_cli_edit_verb_is_gated` exists to hold that — which is
        # why this handler names the exception it will not swallow rather than catching broadly.
        raise
    except (MementoError, OSError) as exc:
        # Everything else becomes a contract exit. An uncaught exception here would hand an agent a
        # traceback and the interpreter's own exit code, which makes "branch on the exit code" a
        # promise this CLI does not keep — a missing `--adapter-file` and a prefix that cannot fit
        # its required section both used to arrive that way.
        outcome = Outcome(
            code=EXIT_USAGE, kind="error", data={"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
    if getattr(args, "json", False):
        # The payload is complete on its own — including any error — so a parsing consumer never
        # has to read stderr to find out what happened.
        sys.stdout.write(json.dumps(outcome.data, indent=2, sort_keys=True, default=str) + "\n")
    else:
        out, err = presentation.render(outcome)
        sys.stdout.write(out)
        sys.stderr.write(err)
    return outcome.code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
