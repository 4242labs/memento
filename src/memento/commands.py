"""The command layer: what each verb *does*, and what it returns (B-02 T8 R1).

Every command here takes parsed arguments, calls the engine, and returns an `Outcome` — an exit
code and a structured payload. **Nothing in this module prints.** Human-facing text lives in
`presentation.py`, and the two are separated for a reason that is not tidiness:

* The exit code and the payload are the **contract** an agent branches on and parses. They are
  worth testing exhaustively, and they can be tested in-process, one function call per case.
* Console prose is **not** contractual. Pinning it line by line buys a suite that breaks on every
  reworded message and catches nothing.

That boundary is also the mutation-ratchet boundary: this module is in scope, `presentation.py` is
out of it by architecture rather than by an exclusion list somebody has to maintain.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
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
from .locking import CasClaim, cas_claims
from .queue import Queue
from .readpath import assemble_prefix, recall
from .spec import load_adapter
from .store import SCHEMA_VERSION, MemoryStore
from .templates import preamble
from .tokenizer import DEFAULT_COUNTER
from .writepath import UNCHECKED, apply_consolidation, facts_fingerprint, read_facts, read_tombstones

#: Exit codes. These are the contract — `docs/agent-consumers.md` tells an agent to branch on them,
#: and `tests/test_exit_codes.py` pins every one against the scenario that produces it.
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_MALFORMED = 2
EXIT_GATE_REJECTED = 3
EXIT_SECRETS = 4
EXIT_STALE = 5
EXIT_CLAIM_HELD = 6
EXIT_DRAIN_REFUSED = 7


@dataclass(frozen=True)
class Outcome:
    """What a command did.

    `code` is what the shell sees. `data` is what `--json` prints — complete on its own, including
    any error, so a parsing consumer never has to read stderr. `kind` names the renderer that turns
    this into human text; it is presentation's business, not this module's.
    """

    code: int = EXIT_OK
    kind: str = "plain"
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.code == EXIT_OK


def _fail(code: int, kind: str, error: str, **extra: Any) -> Outcome:
    return Outcome(code=code, kind=kind, data={"ok": False, "error": error, **extra})


def _ok(kind: str, **data: Any) -> Outcome:
    return Outcome(code=EXIT_OK, kind=kind, data={"ok": True, **data})


def _session_id() -> str:
    return "cli-" + SystemClock().now_iso().replace(":", "").replace("-", "")


def _resolve(ref: str | None) -> Any:
    """Resolve `module:attribute`. A bad reference is a usage error, not a traceback."""
    if not ref:
        return None
    import importlib

    module_name, _, attr = ref.partition(":")
    if not attr:
        raise MementoError(f"expected 'module:attribute', got {ref!r}")
    try:
        obj = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise MementoError(f"cannot resolve adapter {ref!r}: {exc}") from exc
    return obj() if isinstance(obj, type) else obj


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


def _adapter_or_outcome(args: argparse.Namespace, *, required: bool = True) -> tuple[Any, Outcome | None]:
    """`(adapter, None)`, or `(None, outcome)` carrying the refusal.

    Scoped deliberately rather than wrapped around `main`: a blanket handler there would have
    turned `SecretsDetected` from the `edit` verb into a quiet exit code, and that gate raising
    loudly is the behaviour a regression test exists to hold.
    """
    try:
        return _adapter(args, required=required), None
    except MementoError as exc:
        return None, _fail(EXIT_USAGE, "error", str(exc))


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


# --------------------------------------------------------------------- inspection


def cmd_status(args: argparse.Namespace) -> Outcome:
    store = MemoryStore(args.store)
    now = SystemClock().now()
    data: dict[str, Any] = {
        "ok": True,
        "store": str(store.root),
        "schema": SCHEMA_VERSION,
        "documents": list(store.documents()),
        "streams": list(store.streams()),
        "session_logs": len(store.session_logs()),
        "tombstones": len(read_tombstones(store)),
        "backup": {"enabled": is_enabled(store), "remote": read_config(store).remote},
        "claims": [
            {"session": r.session, "pid": r.pid, "stale": r.is_stale(now), "acquired_at": r.acquired_at}
            for r in cas_claims(store.locks_dir)
        ],
    }
    if args.queue:
        queue = Queue(args.queue)
        backlog = queue.backlog()
        data["pending"] = len(queue.pending_sessions())
        data["backlog"] = backlog.message()
    return Outcome(kind="status", data=data)


def cmd_view(args: argparse.Namespace) -> Outcome:
    store = MemoryStore(args.store)
    if not args.document:
        return _ok("document-list", documents=list(store.documents()))
    content = store.read_document(args.document)
    if content is None:
        return _fail(EXIT_USAGE, "error", f"no such document: {args.document}", document=args.document)
    return _ok("document", document=args.document, content=content)


def cmd_facts(args: argparse.Namespace) -> Outcome:
    store = MemoryStore(args.store)
    adapter, refusal = _adapter_or_outcome(args, required=False)
    if refusal is not None:
        return refusal

    if args.from_store:
        # Read the projected documents back into facts and prove the round-trip first. On a store
        # the engine did not write, that proof is the difference between an anti-erosion baseline
        # and a confident misreading of the operator's own memory.
        if adapter is None:
            return _fail(EXIT_USAGE, "error", "--from-store needs --adapter-file or --adapter")
        if getattr(adapter, "facts_from_store", None) is None:
            # Otherwise this returns `{}` and exit 0, which reads as "the store is empty" when it
            # means "this adapter cannot read it" — and an empty baseline is one free erosion.
            return _fail(
                EXIT_USAGE,
                "error",
                f"adapter {getattr(adapter, 'name', '?')!r} cannot parse documents back into facts; "
                "declare one (a spec adapter gets this for free) or write facts_from_store",
            )
        report = check_adoption(store, adapter)
        if not report.ok:
            return _fail(
                EXIT_USAGE,
                "error",
                report.message() or "adoption diverged",
                diverged=list(report.diverged),
                flags=[f.render() for f in report.flags],
            )
        facts = report.facts
    else:
        facts = read_facts(store, adapter)

    if args.fingerprint:
        # The compare-and-swap token. Read it, compose a proposal against these facts, then hand it
        # back to `consolidate --expect` — which is what stops a second writer's work being
        # silently overwritten by a proposal derived from a state that has since moved.
        #
        # Always taken from `read_facts`, never from the `--from-store` parse, because the write
        # path compares against `read_facts` and nothing else. On a store with a `facts.json` the
        # two differ the moment any fact is not projected into a document — the round-trip check
        # still passes, because an unprojected key renders to nothing either way — and the token
        # would then be one no consolidation could ever match. The adoption check above still runs;
        # it just does not get to decide what the token is.
        return _ok("fingerprint", fingerprint=facts_fingerprint(read_facts(store, adapter)))
    return _ok("facts", facts=facts)


def cmd_prefix(args: argparse.Namespace) -> Outcome:
    store = MemoryStore(args.store)
    adapter, refusal = _adapter_or_outcome(args)
    if refusal is not None:
        return refusal
    result = assemble_prefix(store, adapter, budget=args.budget)
    return _ok(
        "prefix",
        text=result.text,
        tokens=result.tokens,
        budget=result.budget,
        counter=result.counter,
        truncated=result.truncated,
        dropped=result.dropped,
        flags=[f.render() for f in result.flags],
    )


def cmd_history(args: argparse.Namespace) -> Outcome:
    store = MemoryStore(args.store)
    revisions = document_revisions(store, args.document, include_abandoned=True)
    return _ok(
        "history",
        document=args.document,
        revisions=[
            {
                "ordinal": rev.ordinal_in_history,
                "ts": rev.event.ts,
                "session": rev.event.session,
                "batch": rev.event.batch,
                "has_prior": rev.has_prior,
                "abandoned": rev.abandoned,
            }
            for rev in revisions
        ],
    )


def cmd_recall(args: argparse.Namespace) -> Outcome:
    """Selective recall, bounded by count *and* by cost.

    An agent pastes this straight into its own context, so `--limit` alone is not a bound: ten hits
    over a long stream is an unpredictable amount of text. The budget comes from the adapter unless
    the caller overrides it, and what the budget cut is reported rather than dropped quietly.
    """
    store = MemoryStore(args.store)
    adapter, refusal = _adapter_or_outcome(args, required=False)
    if refusal is not None:
        return refusal
    budget = args.budget if args.budget is not None else getattr(adapter, "recall_budget_tokens", None)
    result = recall(
        store,
        args.query,
        limit=args.limit,
        streams=args.stream or None,
        sessions=None if args.sessions else (),
        keys=args.key or None,
        since=args.since,
        until=args.until,
        budget=budget,
        counter=getattr(adapter, "token_counter", DEFAULT_COUNTER),
    )
    return _ok(
        "recall",
        hits=[
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
        tokens=result.tokens,
        budget=result.budget,
        counter=result.counter,
        dropped=result.dropped,
        flags=[f.render() for f in result.flags],
    )


def cmd_prompts(args: argparse.Namespace) -> Outcome:
    return _ok("prompts", text=preamble())


# ------------------------------------------------------------------- the write path


def cmd_consolidate(args: argparse.Namespace) -> Outcome:
    """Submit a consolidation from outside Python, through the whole gate stack.

    This is the write path a declarative consumer uses, and it is deliberately the *same* one:
    secrets, schema, derived identity, the anti-erosion floor, ordered scales. A proposal that would
    be refused from a library caller is refused here, all-or-nothing, with every violation reported.
    """
    store = MemoryStore(args.store)
    adapter, refusal = _adapter_or_outcome(args)
    if refusal is not None:
        return refusal
    raw = sys.stdin.read() if args.proposal == "-" else Path(args.proposal).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _fail(EXIT_MALFORMED, "error", f"proposal is not valid JSON: {exc}")
    if not isinstance(payload, dict):
        return _fail(EXIT_MALFORMED, "error", "a proposal must be a JSON object")

    proposal = Proposal(
        facts=dict(payload.get("facts", {})),
        entries={k: list(v) for k, v in dict(payload.get("entries", {})).items()},
        documents=dict(payload.get("documents", {})),
        tombstones=set(payload.get("tombstones", [])),
        session_log=payload.get("session_log"),
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
        return _fail(
            EXIT_GATE_REJECTED,
            "rejected",
            "the gates rejected this consolidation; nothing was written",
            violations=[v.render() for v in exc.violations],
        )
    except SecretsDetected as exc:
        return _fail(EXIT_SECRETS, "rejected", f"secrets: {exc}", violations=[])
    except StaleProposal as exc:
        return _fail(EXIT_STALE, "rejected", str(exc), violations=[])

    return _ok(
        "consolidated",
        documents=list(result.documents_written),
        streams=list(result.streams_written),
        session=args.session,
    )


def cmd_rollback(args: argparse.Namespace) -> Outcome:
    store = MemoryStore(args.store)
    rollback_document(
        store, args.document, session=_session_id(), batch="rollback", revision=args.revision
    )
    return _ok("rolled-back", document=args.document, revision=args.revision)


def cmd_edit(args: argparse.Namespace) -> Outcome:
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
        return _ok("edited", document=args.document, changed=False)
    store.replace_document(args.document, new, session=_session_id(), batch="edit")
    return _ok("edited", document=args.document, changed=True)


def cmd_forget(args: argparse.Namespace) -> Outcome:
    store = MemoryStore(args.store)
    adapter, refusal = _adapter_or_outcome(args, required=False)
    if refusal is not None:
        return refusal
    session = _session_id()
    if adapter is None:
        tombstone(store, args.marker, session=session, batch="forget", reason=args.reason)
        return _ok("forgotten", marker=args.marker, rerendered=False)
    forget_fact(store, adapter, args.marker, session=session, batch="forget", reason=args.reason)
    return _ok("forgotten", marker=args.marker, rerendered=True)


def cmd_backup(args: argparse.Namespace) -> Outcome:
    store = MemoryStore(args.store)
    if not args.yes:
        from .backup import OPT_IN_WARNING

        return _fail(EXIT_USAGE, "backup-refused", "refusing to enable backup without --yes",
                     warning=OPT_IN_WARNING)
    enable_backup(store, acknowledged=True, remote=args.remote, branch=args.branch)
    return _ok("backup-enabled", store=str(store.root), remote=args.remote)


# ------------------------------------------------------- the session lifecycle


def cmd_journal(args: argparse.Namespace) -> Outcome:
    """Append this turn's material, or return what has accumulated.

    The journal is the raw pile a consolidation is later distilled *from*. An agent writes to it as
    the session runs and reads it back at consolidation time — which is the only reason the material
    survives a session at all.
    """
    queue = _queue(args)
    if args.show:
        return _ok("journal", session=args.session, turns=queue.read_journal(args.session))
    if args.text is None:
        return _fail(EXIT_USAGE, "error", "journal needs --text (or - for stdin), or --show")
    text = sys.stdin.read() if args.text == "-" else args.text
    queue.append_turn(args.session, args.turn, {"text": text})
    return _ok("journalled", session=args.session, turn=args.turn)


def cmd_enqueue(args: argparse.Namespace) -> Outcome:
    """Session exit, and the whole of it (ADR D3.2).

    No distillation here, no git, nothing slow — an exit that does real work is an exit the operator
    waits on. Everything expensive happens later, behind the `pending --gate-check` gate.
    """
    _queue(args).close_and_enqueue(args.session)
    return _ok("enqueued", session=args.session)


def cmd_pending(args: argparse.Namespace) -> Outcome:
    """What is waiting, how stale it is — and whether a consolidation may start at all.

    `--gate-check` is the shell's half of `DrainGate`: the same two preconditions the spawning
    parent applies to a drain subprocess, applied to an agent that is about to do the same work in
    its own turn. Consolidating while the read prefix is still being assembled tears the composite
    read, and one-session-stale is fine where inconsistent is not.
    """
    queue = _queue(args)

    if args.gate_check:
        gate = DrainGate(
            prefix_materialized=args.prefix_materialized,
            idle_seconds=args.idle_seconds,
            min_idle_seconds=args.min_idle_seconds,
        )
        refusal = gate.refusal()
        if refusal is not None:
            return _fail(EXIT_DRAIN_REFUSED, "gate-refused", refusal)

    pending = queue.pending_sessions()
    backlog = queue.backlog()
    return _ok(
        "pending",
        pending=[
            {"session": p.session, "enqueued_at": p.enqueued_at, "deferrals": p.deferrals}
            for p in pending
        ],
        backlog={
            "count": backlog.pending,
            "oldest_age_days": backlog.oldest_age_days,
            "breached": backlog.breached,
            "reason": backlog.reason,
            "message": backlog.message(),
        },
    )


def cmd_done(args: argparse.Namespace) -> Outcome:
    """Write the `consolidated` marker — LAST, after the write and after any commit.

    Marker-LAST is the crash-safety invariant: a crash before it means the session is still pending
    and gets re-run, and re-running a consolidation is cheap where losing one is not. So this is its
    own verb rather than a side effect of `consolidate`, which would put it *before* the commit.
    """
    adapter, refusal = _adapter_or_outcome(args, required=False)
    if refusal is not None:
        return refusal
    queue = _queue(args, adapter)
    if not queue.is_enqueued(args.session):
        return _fail(EXIT_USAGE, "error", f"{args.session} was never enqueued", session=args.session)
    queue.mark_consolidated(args.session)
    return _ok("done", session=args.session)


def cmd_claim(args: argparse.Namespace) -> Outcome:
    """Take the session, and return the token that gives it back.

    Held across process boundaries on purpose — an agent's consolidation spans several invocations
    with the model's own thinking in between, and a claim that released when this command exited
    would let a second front-end pay for the same consolidation.
    """
    store = MemoryStore(args.store)
    claim = CasClaim(store.locks_dir, args.session, ttl=args.ttl)
    try:
        record = claim.acquire()
    except ClaimHeld as exc:
        return _fail(EXIT_CLAIM_HELD, "error", str(exc), session=args.session)
    return _ok("claimed", session=args.session, token=record.token, ttl=record.ttl)


def cmd_release(args: argparse.Namespace) -> Outcome:
    store = MemoryStore(args.store)
    claim = CasClaim(store.locks_dir, args.session)
    try:
        released = claim.release(args.token)
    except ClaimHeld as exc:
        return _fail(EXIT_CLAIM_HELD, "error", str(exc), session=args.session)
    return _ok("released", session=args.session, released=released)


def cmd_commit(args: argparse.Namespace) -> Outcome:
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
        return _ok("committed", session=args.session, enabled=False, sha=None, pushed=False)
    sha = backup_mod.commit_consolidation(store, args.session)
    pushed, push_error = False, None
    if args.push:
        # Best-effort, exactly as in the drain: a remote being down must never look like a lost
        # write. The store is the record; git is a copy of it.
        try:
            pushed = backup_mod.push(store)
        except MementoError as exc:
            push_error = str(exc)
    if push_error is not None:
        return _fail(
            EXIT_USAGE, "committed", f"backup push failed: {push_error}",
            session=args.session, enabled=True, sha=sha, pushed=False,
        )
    return _ok("committed", session=args.session, enabled=True, sha=sha, pushed=pushed)
