"""Operator controls (ADR D5/D8): `view`, `edit`, `forget` are first-class verbs, not admin scripts.

Memory the operator cannot inspect and correct is memory they have to trust blindly, so these are
part of the engine rather than something each consumer reinvents. Every write here goes through the
same event log and the same gates as a consolidation — `forget` is honored *because* it writes a
tombstone, not because the CLI is privileged.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .backup import enable_backup, is_enabled, read_config
from .clock import SystemClock
from .forgetting import document_revisions, forget_fact, rollback_document, tombstone
from .queue import Queue
from .readpath import recall
from .store import SCHEMA_VERSION, MemoryStore
from .templates import preamble
from .writepath import read_facts, read_tombstones


def _session_id() -> str:
    return "cli-" + SystemClock().now_iso().replace(":", "").replace("-", "")


def _resolve(ref: str | None) -> Any:
    if not ref:
        return None
    import importlib

    module_name, _, attr = ref.partition(":")
    obj = getattr(importlib.import_module(module_name), attr)
    return obj() if isinstance(obj, type) else obj


def cmd_status(args: argparse.Namespace) -> int:
    store = MemoryStore(args.store)
    print(f"store:          {store.root}")
    print(f"schema:         {SCHEMA_VERSION}")
    print(f"documents:      {', '.join(store.documents()) or '(none)'}")
    print(f"streams:        {', '.join(store.streams()) or '(none)'}")
    print(f"session logs:   {len(store.session_logs())}")
    print(f"tombstones:     {len(read_tombstones(store))}")
    print(f"backup:         {'enabled -> ' + str(read_config(store).remote) if is_enabled(store) else 'off'}")
    if args.queue:
        queue = Queue(args.queue)
        pending = queue.pending_sessions()
        backlog = queue.backlog()
        print(f"pending:        {len(pending)}")
        if backlog.message():
            print(f"FLAG:           {backlog.message()}")
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    store = MemoryStore(args.store)
    if not args.document:
        for name in store.documents():
            print(name)
        return 0
    content = store.read_document(args.document)
    if content is None:
        print(f"no such document: {args.document}", file=sys.stderr)
        return 1
    sys.stdout.write(content)
    return 0


def cmd_facts(args: argparse.Namespace) -> int:
    store = MemoryStore(args.store)
    print(json.dumps(read_facts(store), indent=2, sort_keys=True))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    store = MemoryStore(args.store)
    revisions = document_revisions(store, args.document)
    if not revisions:
        print(f"no recorded revisions for {args.document}")
        return 0
    for rev in revisions:
        ev = rev.event
        marker = "" if rev.has_prior else "  (no prior content — rollback unavailable)"
        if rev.abandoned_predecessor:
            marker += f"  (supersedes abandoned {rev.abandoned_predecessor})"
        print(f"[{rev.ordinal_in_history}] {ev.ts}  session={ev.session}  batch={ev.batch}{marker}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    store = MemoryStore(args.store)
    rollback_document(
        store, args.document, session=_session_id(), batch="rollback", revision=args.revision
    )
    print(f"rolled {args.document} back to revision {args.revision}")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    store = MemoryStore(args.store)
    current = store.read_document(args.document) or ""
    if args.from_file:
        new = Path(args.from_file).read_text(encoding="utf-8")
    else:
        editor = os.environ.get("EDITOR", "vi")
        with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as fh:
            fh.write(current)
            tmp = fh.name
        subprocess.run([editor, tmp], check=True)
        new = Path(tmp).read_text(encoding="utf-8")
        os.unlink(tmp)
    if new == current:
        print("unchanged")
        return 0
    store.replace_document(args.document, new, session=_session_id(), batch="edit")
    print(f"{args.document} updated; prior content kept in the document_replaced history")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    store = MemoryStore(args.store)
    adapter = _resolve(args.adapter)
    session = _session_id()
    if adapter is None:
        tombstone(store, args.marker, session=session, batch="forget", reason=args.reason)
        print(f"tombstoned {args.marker}; it will be honored by every future fold and consolidation")
        print("(no adapter given, so the projected documents were not re-rendered)")
        return 0
    forget_fact(store, adapter, args.marker, session=session, batch="forget", reason=args.reason)
    print(f"forgot {args.marker} and re-rendered the projected documents")
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    store = MemoryStore(args.store)
    hits = recall(store, args.query, limit=args.limit)
    if not hits:
        print("no matches")
        return 0
    for hit in hits:
        where = f"{hit.location}:{hit.entry_id}" if hit.entry_id else hit.location
        print(f"[{hit.score}] {where}  {hit.text}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    store = MemoryStore(args.store)
    if not args.yes:
        from .backup import OPT_IN_WARNING

        print(f"Refusing to enable backup without --yes.\n\n{OPT_IN_WARNING}", file=sys.stderr)
        return 1
    enable_backup(store, acknowledged=True, remote=args.remote, branch=args.branch)
    print(f"backup enabled for {store.root} -> {args.remote or '(local git only)'}")
    return 0


def cmd_prompts(args: argparse.Namespace) -> int:
    sys.stdout.write(preamble())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memento", description=__doc__)
    parser.add_argument("--store", required=True, help="path to the store root")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="what this store holds right now")
    p.add_argument("--queue", default=None)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("view", help="print a projected document, or list them")
    p.add_argument("document", nargs="?")
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("facts", help="print the structured facts behind the documents")
    p.set_defaults(func=cmd_facts)

    p = sub.add_parser("history", help="revisions of a document")
    p.add_argument("document")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("rollback", help="restore a document's prior content")
    p.add_argument("document")
    p.add_argument("--revision", type=int, default=-1)
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("edit", help="edit a document; the prior content is kept")
    p.add_argument("document")
    p.add_argument("--from-file", default=None)
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("forget", help="tombstone a fact; never a delete")
    p.add_argument("marker", help="path/key as reported by the gates, e.g. languages/de")
    p.add_argument("--adapter", default=None, help="module:attribute, to re-render documents")
    p.add_argument("--reason", default="operator forget")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("recall", help="search events and documents")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("backup", help="opt this store into git backup")
    p.add_argument("--remote", default=None)
    p.add_argument("--branch", default="main")
    p.add_argument("--yes", action="store_true", help="acknowledge the warning")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("prompts", help="print the engine's pinned relationship/restraint templates")
    p.set_defaults(func=cmd_prompts)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
