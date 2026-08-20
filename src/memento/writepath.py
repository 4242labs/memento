"""The validated write path (ADR D3.1).

All-or-nothing: a consolidation passes every gate or nothing is written at all. A failure marks the
session `deferred` and raises a FLAG — it never writes the half that passed, because a half-applied
consolidation is exactly the silent erosion the gates exist to prevent.

The structured facts live in `.memento/facts.json`, written as a projected document like any other,
so they carry `document_replaced` history and roll back the same way. That file is what makes the
anti-erosion floor checkable without parsing markdown back into data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

from .errors import GateFailure, MementoError, SecretsDetected, StaleProposal
from .events import EVENT_RETIRED
from .flags import GATE_REJECTED, SECRETS_REJECTED, STALE_PROPOSAL, Flag, FlagSink
from .fold import RETIRED, fold
from .gates import Proposal, StoreState
from .locking import StoreLock
from .queue import Queue
from .secrets import scan_many
from .store import DocumentWrite, MemoryStore, sha256_text

#: Explicit opt-out for `expected_fingerprint`. A caller that genuinely has no baseline — the first
#: write to an empty store, a test — passes this. Silence is not an accepted answer, because the
#: caller who forgets is exactly the caller whose write gets lost.
UNCHECKED = "unchecked"

#: Sentinel meaning "the caller said nothing", distinct from UNCHECKED meaning "the caller opted out".
_REQUIRED = "<required>"

FACTS_DOCUMENT = ".memento/facts.json"
TOMBSTONE_STREAM = ".memento/tombstones"


@dataclass
class WriteResult:
    ok: bool
    session: str
    batch: str
    streams_written: list[str] = field(default_factory=list)
    documents_written: list[str] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)


def read_facts(store: MemoryStore, adapter: Any = None) -> dict[str, Any]:
    """Current structured facts.

    Falls back to an adapter-supplied parse of the existing documents, which is how a store that
    predates the engine — jubs' today — gets a real anti-erosion baseline on its very first
    consolidation instead of an empty one.
    """
    raw = store.read_document(FACTS_DOCUMENT)
    if raw is not None:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if adapter is not None and getattr(adapter, "facts_from_store", None) is not None:
        return dict(adapter.facts_from_store(store))
    return {}


def read_tombstones(store: MemoryStore) -> set[str]:
    folded = fold(store.log(TOMBSTONE_STREAM).read())
    return {k for k, v in folded.items() if v.status == RETIRED}


def current_state(store: MemoryStore, adapter: Any = None) -> StoreState:
    return StoreState(facts=read_facts(store, adapter), tombstones=read_tombstones(store))


def render_facts(facts: dict[str, Any]) -> str:
    return json.dumps(facts, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def facts_fingerprint(facts: dict[str, Any]) -> str:
    """A stable digest of the facts a proposal was derived from.

    The distiller runs with no lock held — deliberately — so between reading the state and writing
    the result another drain may have landed. Comparing fingerprints under the lock is what turns
    that from a silent last-writer-wins overwrite into a refusal and a redrive.
    """
    return sha256_text(render_facts(facts))


def apply_consolidation(
    store: MemoryStore,
    adapter: Any,
    proposal: Proposal,
    *,
    session: str,
    batch: str,
    turn: int | None = None,
    lock: StoreLock | None = None,
    queue: Queue | None = None,
    sink: FlagSink | None = None,
    expected_fingerprint: str = _REQUIRED,
) -> WriteResult:
    """Gate a consolidation and, if it passes cleanly, write it under the store lock.

    `expected_fingerprint` is the digest of the facts this proposal was derived from — get it from
    `facts_fingerprint(state.facts)` on the state you passed to the distiller. The write is refused
    if the store moved underneath it.

    It is **required**, and `UNCHECKED` is the explicit opt-out. Defaulting it to "check nothing"
    made concurrent drains silently last-writer-wins; defaulting it to "check the state we gated
    against" narrowed the window without closing it, and both defaults hid the decision from the one
    person able to make it.
    """
    if expected_fingerprint is _REQUIRED:
        raise MementoError(
            "apply_consolidation requires expected_fingerprint: pass "
            "facts_fingerprint(state.facts) for the state this proposal was derived from, or "
            "writepath.UNCHECKED to opt out deliberately"
        )
    sink = sink if sink is not None else FlagSink()
    lock = lock or StoreLock.for_store(store.locks_dir)

    documents = dict(adapter.render_documents(proposal.facts))
    documents.update(proposal.documents)

    found = scan_many(
        [
            *((f"document:{name}", text) for name, text in documents.items()),
            ("facts", render_facts(proposal.facts)),
            ("session-log", proposal.session_log or ""),
            *(
                (f"{stream}[{i}]", json.dumps(entry, ensure_ascii=False))
                for stream, entries in proposal.entries.items()
                for i, entry in enumerate(entries)
            ),
        ]
    )
    if found:
        message = "; ".join(m.render() for m in found)
        sink.raise_flag(SECRETS_REJECTED, message, session=session)
        if queue is not None:
            queue.mark_deferred(session, f"secrets: {message}")
        raise SecretsDetected(message)

    gated_against = current_state(store, adapter)
    try:
        # Rules judge what will actually be written. `proposal.documents` is documented as "that
        # rendering" but arrives as an override map in the standard flow, so a rule reading it —
        # DocumentBudgetRule, or an adapter's own — would gate an empty dict while the real
        # projection ships unexamined.
        adapter.rule_set().enforce(gated_against, replace(proposal, documents=documents))
    except GateFailure as exc:
        sink.raise_flag(GATE_REJECTED, exc.render(), session=session)
        if queue is not None:
            queue.mark_deferred(session, exc.render())
        raise

    with lock.hold():
        if expected_fingerprint != UNCHECKED:
            current = facts_fingerprint(read_facts(store, adapter))
            if current != expected_fingerprint:
                message = (
                    "the store changed while this consolidation was being produced; "
                    "redrive against the current state"
                )
                sink.raise_flag(STALE_PROPOSAL, message, session=session)
                if queue is not None:
                    queue.mark_deferred(session, message)
                raise StaleProposal(message)

        streams_written = []
        for stream, entries in proposal.entries.items():
            if not entries:
                continue
            store.append(stream, entries, session=session, batch=batch, turn=turn)
            streams_written.append(stream)

        if proposal.tombstones:
            store.append(
                TOMBSTONE_STREAM,
                [
                    {"id": marker, "event": EVENT_RETIRED, "reason": "consolidation"}
                    for marker in sorted(proposal.tombstones)
                ],
                session=session,
                batch=batch,
                turn=turn,
            )
            streams_written.append(TOMBSTONE_STREAM)

        writes = [DocumentWrite(FACTS_DOCUMENT, render_facts(proposal.facts))]
        writes += [DocumentWrite(name, text) for name, text in sorted(documents.items())]
        store.replace_documents(writes, session=session, batch=batch, turn=turn)

        if proposal.session_log is not None:
            store.write_session_log(session, proposal.session_log)

    return WriteResult(
        ok=True,
        session=session,
        batch=batch,
        streams_written=streams_written,
        documents_written=[w.name for w in writes],
        flags=list(sink.flags),
    )
