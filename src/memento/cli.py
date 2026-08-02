"""Operator controls (ADR D5/D8), and the whole write path for a consumer with no Python.

Memory the operator cannot inspect and correct is memory they have to trust blindly, so `view`,
`edit` and `forget` are part of the engine rather than something each consumer reinvents. Every
write here goes through the same event log and the same gates as a consolidation — `forget` is
honored *because* it writes a tombstone, not because the CLI is privileged.

The second consumer class is an **agent**: markdown and a shell, no import statement anywhere. It
needs more than operator controls — it needs the session lifecycle too (`journal`, `enqueue`,
`pending`, `claim`/`release`, `commit`), because for that consumer this CLI *is* the API. What it
does not get is a weaker engine: the gates, the compare-and-swap and the drain gate all apply
through the shell exactly as they apply to a library caller. See `docs/agent-consumers.md`.

Exit codes are part of that contract — an agent branches on them:

    0  fine        3  gates rejected it     5  the store moved underneath it (redrive)
    1  usage/IO    4  secrets                6  another claimant holds the session
                                             7  the drain gate refuses: not yet
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

from . import backup as backup_mod
from .adoption import check_adoption
from .backup import enable_backup, is_enabled, read_config
from .clock import SystemClock
from .drain import DrainGate
from .errors import ClaimHeld, GateFailure, MementoError, SecretsDetected, StaleProposal
from .forgetting import document_revisions, forget_fact, rollback_document, tombstone
from .gates import Proposal
from .locking import DEFAULT_CLAIM_TTL, CasClaim, cas_claims
from .queue import Queue
from .readpath import assemble_prefix, recall
from .spec import load_adapter
from .store import SCHEMA_VERSION, MemoryStore
from .templates import preamble
from .tokenizer import DEFAULT_COUNTER
from .writepath import UNCHECKED, apply_consolidation, facts_fingerprint, read_facts, read_tombstones

EXIT_GATE_REJECTED = 3
EXIT_SECRETS = 4
EXIT_STALE = 5
EXIT_CLAIM_HELD = 6
EXIT_DRAIN_REFUSED = 7


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
    now = SystemClock().now()
    for record in cas_claims(store.locks_dir):
        # A claim outliving its holder is the cost of making it valid across processes, so the
        # operator gets to see who holds what — and, for a stale one, that anyone may take it.
        state = "stale, reclaimable" if record.is_stale(now) else f"pid {record.pid}"
        print(f"claim:          {record.session} ({state})")
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


def _adapter(args: argparse.Namespace, *, required: bool = True) -> Any:
    """The adapter for this invocation: a declared spec file, or a Python `module:attribute`.

    A consumer with no Python of its own — an agent driven by markdown and a shell — declares one.
    Both paths build the same `Adapter`, so both get the same gates.
    """
    adapter = None
    if getattr(args, "adapter_file", None):
        adapter = load_adapter(args.adapter_file)
    elif getattr(args, "adapter", None):
        adapter = _resolve(args.adapter)
    if adapter is None and required:
        raise MementoError("this command needs --adapter-file or --adapter")
    return adapter


def _adapter_or_report(args: argparse.Namespace, *, required: bool = True) -> tuple[Any, int | None]:
    """`(adapter, None)`, or `(None, exit_code)` with the reason already on stderr.

    Scoped deliberately rather than wrapped around `main`: a blanket handler there would have
    turned `SecretsDetected` from the `edit` verb into a quiet exit code, and that gate raising
    loudly is the behaviour a regression test exists to hold.
    """
    try:
        return _adapter(args, required=required), None
    except MementoError as exc:
        print(str(exc), file=sys.stderr)
        return None, 1


def cmd_facts(args: argparse.Namespace) -> int:
    store = MemoryStore(args.store)
    adapter, code = _adapter_or_report(args, required=False)
    if code is not None:
        return code
    if args.from_store:
        # Read the projected documents back into facts and prove the round-trip first. On a store
        # the engine did not write, that proof is the difference between an anti-erosion baseline
        # and a confident misreading of the operator's own memory.
        if adapter is None:
            print("--from-store needs --adapter-file or --adapter", file=sys.stderr)
            return 1
        if getattr(adapter, "facts_from_store", None) is None:
            # Otherwise this prints `{}` and exits 0, which reads as "the store is empty" when it
            # means "this adapter cannot read it" — and an empty baseline is one free erosion.
            print(
                f"adapter {getattr(adapter, 'name', '?')!r} cannot parse documents back into facts; "
                "declare one (a spec adapter gets this for free) or write facts_from_store",
                file=sys.stderr,
            )
            return 1
        report = check_adoption(store, adapter)
        for flag in report.flags:
            print(f"FLAG: {flag.render()}", file=sys.stderr)
        if not report.ok:
            return 1
        facts = report.facts
    else:
        facts = read_facts(store, adapter)
    if args.fingerprint:
        # The compare-and-swap token. Read it, compose a proposal against these facts, then hand it
        # back to `consolidate --expect` — which is what stops a second writer's work being
        # silently overwritten by a proposal derived from a state that has since moved.
        print(facts_fingerprint(facts))
        return 0
    print(json.dumps(facts, indent=2, sort_keys=True))
    return 0


def cmd_prefix(args: argparse.Namespace) -> int:
    store = MemoryStore(args.store)
    adapter, code = _adapter_or_report(args)
    if code is not None:
        return code
    result = assemble_prefix(store, adapter, budget=args.budget)
    if args.json:
        print(
            json.dumps(
                {
                    "text": result.text,
                    "tokens": result.tokens,
                    "budget": result.budget,
                    "counter": result.counter,
                    "truncated": result.truncated,
                    "dropped": result.dropped,
                    "flags": [f.render() for f in result.flags],
                },
                indent=2,
            )
        )
        return 0
    for flag in result.flags:
        print(f"FLAG: {flag.render()}", file=sys.stderr)
    sys.stdout.write(result.text + ("\n" if result.text else ""))
    return 0


def cmd_consolidate(args: argparse.Namespace) -> int:
    """Submit a consolidation from outside Python, through the whole gate stack.

    This is the write path a declarative consumer uses, and it is deliberately the *same* one:
    secrets, schema, derived identity, the anti-erosion floor, ordered scales. A proposal that would
    be refused from a library caller is refused here, all-or-nothing, with every violation printed.
    """
    store = MemoryStore(args.store)
    adapter, code = _adapter_or_report(args)
    if code is not None:
        return code
    raw = sys.stdin.read() if args.proposal == "-" else Path(args.proposal).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"proposal is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print("a proposal must be a JSON object", file=sys.stderr)
        return 2

    proposal = Proposal(
        facts=dict(data.get("facts", {})),
        entries={k: list(v) for k, v in dict(data.get("entries", {})).items()},
        documents=dict(data.get("documents", {})),
        tombstones=set(data.get("tombstones", [])),
        session_log=data.get("session_log"),
    )
    try:
        result = apply_consolidation(
            store,
            adapter,
            proposal,
            session=args.session,
            batch=args.batch,
            # Given a queue, a rejection marks the session deferred instead of vanishing into an
            # exit code — the same bookkeeping the drain does, and what keeps the backlog FLAG
            # honest for a consumer whose whole write path is this command.
            queue=Queue(args.queue) if args.queue else None,
            expected_fingerprint=UNCHECKED if args.unchecked else args.expect,
        )
    except GateFailure as exc:
        print("REJECTED — nothing was written.", file=sys.stderr)
        for violation in exc.violations:
            print(f"  {violation.render()}", file=sys.stderr)
        return EXIT_GATE_REJECTED
    except SecretsDetected as exc:
        print(f"REJECTED — secrets: {exc}", file=sys.stderr)
        return EXIT_SECRETS
    except StaleProposal as exc:
        print(f"REJECTED — {exc}", file=sys.stderr)
        return EXIT_STALE

    print(f"wrote documents: {', '.join(result.documents_written)}")
    if result.streams_written:
        print(f"wrote streams:   {', '.join(result.streams_written)}")
    return 0


# --------------------------------------------------- the session lifecycle, from a shell


def _queue(args: argparse.Namespace, adapter: Any = None) -> Queue:
    """The queue, carrying the adapter's retention policy when the caller has one.

    Retention is an adapter decision the consumer states out loud, and the only verb that acts on it
    is `done` — marking a session consolidated is what makes its transcript material eligible for
    pruning. A queue built without the adapter silently keeps everything, which is the safe default
    and the wrong answer for a consumer that declared otherwise.
    """
    if adapter is not None and getattr(adapter, "retention", None) is not None:
        return Queue(args.queue, retention=adapter.retention)
    return Queue(args.queue)


def cmd_journal(args: argparse.Namespace) -> int:
    """Append this turn's material, or print what has accumulated.

    The journal is the raw pile a consolidation is later distilled *from*. An agent writes to it as
    the session runs and reads it back at consolidation time — which is the only reason the material
    survives a session at all.
    """
    queue = _queue(args)
    if args.show:
        for record in queue.read_journal(args.session):
            print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        return 0
    if args.text is None:
        print("journal needs --text (or - for stdin), or --show", file=sys.stderr)
        return 1
    text = sys.stdin.read() if args.text == "-" else args.text
    queue.append_turn(args.session, args.turn, {"text": text})
    return 0


def cmd_enqueue(args: argparse.Namespace) -> int:
    """Session exit, and the whole of it (ADR D3.2).

    No distillation here, no git, nothing slow — an exit that does real work is an exit the operator
    waits on. Everything expensive happens later, behind the `pending --gate-check` gate.
    """
    _queue(args).close_and_enqueue(args.session)
    print(f"enqueued {args.session}; consolidate it later, after `pending --gate-check` passes")
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    """What is waiting, how stale it is — and whether a consolidation may start at all.

    `--gate-check` is the shell's half of `DrainGate`: the same two preconditions the spawning
    parent applies to a drain subprocess, applied to an agent that is about to do the same work in
    its own turn. Consolidating while the read prefix is still being assembled tears the composite
    read, and one-session-stale is fine where inconsistent is not.
    """
    queue = _queue(args)
    pending = queue.pending_sessions()

    if args.gate_check:
        gate = DrainGate(
            prefix_materialized=args.prefix_materialized,
            idle_seconds=args.idle_seconds,
            min_idle_seconds=args.min_idle_seconds,
        )
        refusal = gate.refusal()
        if refusal is not None:
            print(f"REFUSED — {refusal}", file=sys.stderr)
            return EXIT_DRAIN_REFUSED

    backlog = queue.backlog()
    if args.json:
        print(
            json.dumps(
                {
                    "pending": [
                        {"session": p.session, "enqueued_at": p.enqueued_at, "deferrals": p.deferrals}
                        for p in pending
                    ],
                    "backlog": {
                        "count": backlog.pending,
                        "oldest_age_days": backlog.oldest_age_days,
                        "breached": backlog.breached,
                        "reason": backlog.reason,
                    },
                },
                indent=2,
            )
        )
        return 0

    for item in pending:
        deferred = f"  deferrals={item.deferrals}" if item.deferrals else ""
        print(f"{item.session}  enqueued_at={item.enqueued_at:.0f}{deferred}")
    if not pending:
        print("nothing pending")
    if backlog.message():
        # Deferred must never mean forgotten (ADR D3.5). This is that surface, for a consumer whose
        # only view of the queue is this command.
        print(f"FLAG: {backlog.message()}", file=sys.stderr)
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    """Write the `consolidated` marker — LAST, after the write and after any commit.

    Marker-LAST is the crash-safety invariant: a crash before it means the session is still pending
    and gets re-run, and re-running a consolidation is cheap where losing one is not. So this is its
    own verb rather than a side effect of `consolidate`, which would put it *before* the commit.
    """
    adapter, code = _adapter_or_report(args, required=False)
    if code is not None:
        return code
    queue = _queue(args, adapter)
    if not queue.is_enqueued(args.session):
        print(f"{args.session} was never enqueued", file=sys.stderr)
        return 1
    queue.mark_consolidated(args.session)
    print(f"marked {args.session} consolidated")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    """Take the session, and print the token that gives it back.

    Held across process boundaries on purpose — an agent's consolidation spans several invocations
    with the model's own thinking in between, and a claim that released when this command exited
    would let a second front-end pay for the same consolidation.
    """
    store = MemoryStore(args.store)
    claim = CasClaim(store.locks_dir, args.session, ttl=args.ttl)
    try:
        record = claim.acquire()
    except ClaimHeld as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CLAIM_HELD
    print(record.token)
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    store = MemoryStore(args.store)
    claim = CasClaim(store.locks_dir, args.session)
    try:
        released = claim.release(args.token)
    except ClaimHeld as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CLAIM_HELD
    print(f"released {args.session}" if released else f"{args.session} was not claimed")
    return 0


def cmd_commit(args: argparse.Namespace) -> int:
    """Commit — and by default push — a consolidation on a backup-enabled store (ADR D3.4/D8).

    The drain does this for a library consumer. An agent consolidates in its own process, so without
    this verb a store that opted into backup would accumulate consolidations that never leave the
    machine. Attribution is the consolidated session's, never a later one's.
    """
    store = MemoryStore(args.store)
    if not is_enabled(store):
        # Not an error: backup is opt-in, and a store that did not opt in is in a state its owner
        # chose. Failing here would make every session on such a store end in a non-zero exit, which
        # is how a shell loop learns to stop reading exit codes.
        print("backup is not enabled for this store; nothing to commit")
        return 0
    sha = backup_mod.commit_consolidation(store, args.session)
    print(f"committed {sha}" if sha else "nothing to commit")
    if args.push:
        # Best-effort, exactly as in the drain: a remote being down must never look like a lost
        # write. The store is the record; git is a copy of it.
        try:
            pushed = backup_mod.push(store)
        except MementoError as exc:
            print(f"FLAG: backup push failed: {exc}", file=sys.stderr)
            return 1
        print("pushed" if pushed else "no remote configured; committed locally only")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    store = MemoryStore(args.store)
    revisions = document_revisions(store, args.document, include_abandoned=True)
    if not revisions:
        print(f"no recorded revisions for {args.document}")
        return 0
    for rev in revisions:
        ev = rev.event
        marker = "" if rev.has_prior else "  (no prior content — rollback unavailable)"
        if rev.abandoned:
            marker += "  (abandoned — recorded but never written)"
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
    adapter = _adapter(args, required=False)
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
    """Selective recall, bounded by count *and* by cost.

    An agent pastes this straight into its own context, so `--limit` alone is not a bound: ten hits
    over a long stream is an unpredictable amount of text. The budget comes from the adapter unless
    the caller overrides it, and what the budget cut is reported rather than dropped quietly.
    """
    store = MemoryStore(args.store)
    adapter, code = _adapter_or_report(args, required=False)
    if code is not None:
        return code
    budget = args.budget if args.budget is not None else getattr(adapter, "recall_budget_tokens", None)
    result = recall(
        store,
        args.query,
        limit=args.limit,
        streams=args.stream or None,
        keys=args.key or None,
        since=args.since,
        until=args.until,
        budget=budget,
        counter=getattr(adapter, "token_counter", DEFAULT_COUNTER),
    )
    if args.json:
        print(
            json.dumps(
                {
                    "hits": [
                        {
                            "source": h.source,
                            "location": h.location,
                            "entry_id": h.entry_id,
                            "text": h.text,
                            "score": h.score,
                            "ts": h.ts,
                        }
                        for h in result.hits
                    ],
                    "tokens": result.tokens,
                    "budget": result.budget,
                    "counter": result.counter,
                    "dropped": result.dropped,
                    "flags": [f.render() for f in result.flags],
                },
                indent=2,
            )
        )
        return 0
    for flag in result.flags:
        print(f"FLAG: {flag.render()}", file=sys.stderr)
    if not result.hits:
        print("no matches")
        return 0
    for hit in result.hits:
        print(hit.render())
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


def _adapter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--adapter", default=None, help="module:attribute")
    parser.add_argument("--adapter-file", default=None, help="path to a declared adapter spec (JSON)")


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
    p.add_argument("--fingerprint", action="store_true", help="print only the compare-and-swap token")
    p.add_argument(
        "--from-store",
        action="store_true",
        help="parse the projected documents back into facts; refuses if the bytes do not round-trip",
    )
    _adapter_args(p)
    p.set_defaults(func=cmd_facts)

    p = sub.add_parser("prefix", help="assemble the budgeted core prefix for a session")
    p.add_argument("--budget", type=int, default=None)
    p.add_argument("--json", action="store_true")
    _adapter_args(p)
    p.set_defaults(func=cmd_prefix)

    p = sub.add_parser("consolidate", help="submit a proposal through the write gates")
    p.add_argument("--proposal", required=True, help="path to a proposal JSON, or - for stdin")
    p.add_argument("--session", required=True)
    p.add_argument("--batch", default="consolidate")
    p.add_argument("--queue", default=None, help="mark the session deferred if this is rejected")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--expect", help="fingerprint from `facts --fingerprint`")
    group.add_argument(
        "--unchecked",
        action="store_true",
        help="opt out of the compare-and-swap, deliberately (first write to an empty store)",
    )
    _adapter_args(p)
    p.set_defaults(func=cmd_consolidate)

    p = sub.add_parser("journal", help="append this turn's material, or print what accumulated")
    p.add_argument("session")
    p.add_argument("--queue", required=True)
    p.add_argument("--turn", type=int, default=0)
    p.add_argument("--text", default=None, help="the turn's material, or - for stdin")
    p.add_argument("--show", action="store_true", help="print the journal instead of appending")
    p.set_defaults(func=cmd_journal)

    p = sub.add_parser("enqueue", help="session exit: close the journal and mark it pending")
    p.add_argument("session")
    p.add_argument("--queue", required=True)
    p.set_defaults(func=cmd_enqueue)

    p = sub.add_parser("pending", help="what is waiting, and whether consolidation may start")
    p.add_argument("--queue", required=True)
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--gate-check",
        action="store_true",
        help="refuse (exit 7) unless the drain gate's preconditions hold",
    )
    p.add_argument("--idle-seconds", type=float, default=0.0)
    p.add_argument("--min-idle-seconds", type=float, default=5.0)
    p.add_argument("--prefix-materialized", action="store_true")
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("done", help="write the consolidated marker — last, after commit")
    p.add_argument("session")
    p.add_argument("--queue", required=True)
    _adapter_args(p)  # only for its retention policy: pruning is gated on the marker this writes
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("claim", help="claim a session's consolidation; prints the release token")
    p.add_argument("session")
    p.add_argument("--ttl", type=float, default=DEFAULT_CLAIM_TTL)
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("release", help="give a claimed session back")
    p.add_argument("session")
    p.add_argument("--token", required=True)
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("commit", help="commit and push a consolidation on a backup-enabled store")
    p.add_argument("--session", required=True)
    p.add_argument("--no-push", dest="push", action="store_false", default=True)
    p.set_defaults(func=cmd_commit)

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
    p.add_argument("--reason", default="operator forget")
    _adapter_args(p)
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("recall", help="search events and documents")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--budget", type=int, default=None, help="token ceiling; adapter's if unset")
    p.add_argument("--stream", action="append", default=[], help="restrict to a stream; repeatable")
    p.add_argument("--key", action="append", default=[], help="restrict to an entry id; repeatable")
    p.add_argument("--since", default=None, help="ISO-8601 lower bound on an entry's last-seen time")
    p.add_argument("--until", default=None, help="ISO-8601 upper bound")
    p.add_argument("--json", action="store_true")
    _adapter_args(p)
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
