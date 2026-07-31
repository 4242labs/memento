"""Forgetting: tombstone, never delete (ADR D5).

Retirement is an event. A tombstoned entry stays visible to the fold — the anti-erosion gate needs
to see it to allow a set to shrink — but recall stops surfacing it and every future consolidation
honors it.

Reconsolidation-on-retrieval lives here too: something surfaced and contradicted live gets a
`contradicted` event now, and the next consolidation is expected to correct it.

TTL decay of *nominations* is allowed elsewhere; TTL deletion of data is not, and there is no
function in this module that removes an event.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Sequence

from .errors import MementoError
from .events import EVENT_CONTRADICTED, EVENT_RETIRED, Event
from .gates import DEFAULT_IDENTITY_KEYS, Proposal, get, member_key, parse_declared
from .store import MemoryStore
from .writepath import TOMBSTONE_STREAM, apply_consolidation, facts_fingerprint, read_facts


def tombstone(
    store: MemoryStore,
    marker: str,
    *,
    session: str,
    batch: str,
    reason: str = "operator forget",
) -> list[Event]:
    """Record a tombstone marker. Honored by every future fold, gate, and consolidation."""
    return store.append(
        TOMBSTONE_STREAM,
        [{"id": marker, "event": EVENT_RETIRED, "reason": reason}],
        session=session,
        batch=batch,
    )


def retire_entry(
    store: MemoryStore,
    stream: str,
    entry_id: str,
    *,
    session: str,
    batch: str,
    reason: str = "operator forget",
) -> list[Event]:
    """Retire one event-log entry, and record the matching tombstone marker."""
    events = store.append(
        stream,
        [{"id": entry_id, "event": EVENT_RETIRED, "reason": reason}],
        session=session,
        batch=batch,
    )
    tombstone(store, f"{stream}/{entry_id}", session=session, batch=f"{batch}-tombstone", reason=reason)
    return events


def note_contradiction(
    store: MemoryStore,
    stream: str,
    entry_id: str,
    *,
    session: str,
    batch: str,
    note: str = "",
) -> list[Event]:
    """Reconsolidation-on-retrieval: mark a surfaced entry as contradicted in conversation.

    The fold flags the entry `contested`; correcting it is the next consolidation's job, not this
    call's — nothing is rewritten here.
    """
    return store.append(
        stream,
        [{"id": entry_id, "event": EVENT_CONTRADICTED, "note": note}],
        session=session,
        batch=batch,
    )


def drop_member(
    facts: dict[str, Any],
    marker: str,
    identity_keys: Sequence[str] = DEFAULT_IDENTITY_KEYS,
) -> dict[str, Any]:
    """Return a copy of `facts` with the member named by `path/key` removed.

    `marker` is the same string the anti-erosion floor reports and the tombstone records, so the
    thing the operator forgets and the thing the gate permits are literally the same identifier —
    and it is resolved by the same traversal, so a key containing a `.` behaves here exactly as it
    does there.
    """
    out = copy.deepcopy(facts)
    path_str, _, key = marker.rpartition("/")
    node: Any = out
    for part in parse_declared(path_str):
        found, node = get(node, (part,), identity_keys)
        if not found:
            raise MementoError(f"no such facts path: {marker}")

    if isinstance(node, dict):
        if key not in node:
            raise MementoError(f"no such member: {marker}")
        del node[key]
    elif isinstance(node, list):
        keep = [item for item in node if member_key(item, identity_keys) != key]
        if len(keep) == len(node):
            raise MementoError(f"no such member: {marker}")
        node.clear()
        node.extend(keep)
    else:
        raise MementoError(f"{marker} does not name a collection member")
    return out


def forget_fact(
    store: MemoryStore,
    adapter: Any,
    marker: str,
    *,
    session: str,
    batch: str,
    reason: str = "operator forget",
) -> Any:
    """Operator `forget` on a projected fact.

    Writes the tombstone first, then re-renders through the normal gated write path — which is the
    point: the gates that refuse an LLM's unexplained deletion accept this one *because* the
    tombstone exists, rather than because the caller was trusted.
    """
    tombstone(store, marker, session=session, batch=f"{batch}-tombstone", reason=reason)
    current = read_facts(store, adapter)
    facts = drop_member(current, marker, getattr(adapter, "identity_keys", DEFAULT_IDENTITY_KEYS))
    proposal = Proposal(facts=facts, tombstones={marker})
    return apply_consolidation(
        store,
        adapter,
        proposal,
        session=session,
        batch=batch,
        expected_fingerprint=facts_fingerprint(current),
    )


# ------------------------------------------------------------------ rollback


@dataclass(frozen=True)
class Revision:
    event: Event
    ordinal_in_history: int
    has_prior: bool


def document_revisions(store: MemoryStore, name: str) -> list[Revision]:
    history = store.document_history(name)
    out = []
    for i, ev in enumerate(history):
        has_prior = ev.payload.get("prior_content") is not None or "prior_ref" in ev.payload
        out.append(Revision(event=ev, ordinal_in_history=i, has_prior=has_prior))
    return out


def rollback_document(
    store: MemoryStore,
    name: str,
    *,
    session: str,
    batch: str,
    revision: int = -1,
) -> str:
    """Restore the content a `document_replaced` event superseded.

    `revision` indexes `document_revisions`; the default rolls back the most recent replace. The
    restore is itself a replace, so the rollback is in the history too — nothing is unwound quietly.
    """
    revisions = document_revisions(store, name)
    if not revisions:
        raise MementoError(f"no recorded revisions for document {name!r}")
    chosen = revisions[revision]
    if not chosen.has_prior:
        raise MementoError(
            f"revision {revision} of {name!r} recorded no prior content; rollback is unavailable"
        )
    prior = store.prior_content(chosen.event)
    if prior is None:
        raise MementoError(f"revision {revision} of {name!r} has no prior content to restore")
    store.replace_document(name, prior, session=session, batch=batch)
    return prior


__all__ = [
    "Revision",
    "document_revisions",
    "drop_member",
    "forget_fact",
    "note_contradiction",
    "retire_entry",
    "rollback_document",
    "tombstone",
]
