"""Status is never stored — it is folded from event history at read time (ADR D2).

Supersession and retirement are events. Nothing in this module mutates a file; the fold is a pure
function of the log, which is what keeps crash recovery idempotent.

Retired entries stay *visible* to the fold on purpose (ADR D5): the anti-erosion gate needs to see a
tombstone to allow a set to shrink, so a tombstone that vanished from the fold would silently
re-open the erosion hole it was written to close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .events import (
    EVENT_CONTRADICTED,
    EVENT_ENTRY,
    EVENT_RETIRED,
    EVENT_SUPERSEDED_BY,
    Event,
)

ACTIVE = "active"
SUPERSEDED = "superseded"
RETIRED = "retired"


@dataclass
class FoldedEntry:
    id: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = ACTIVE
    superseded_by: str | None = None
    retired_reason: str | None = None
    contested: bool = False
    first_seen: str | None = None
    last_seen: str | None = None
    observations: int = 0

    @property
    def is_active(self) -> bool:
        return self.status == ACTIVE

    @property
    def is_tombstoned(self) -> bool:
        return self.status == RETIRED


def fold(events: Iterable[Event]) -> dict[str, FoldedEntry]:
    """Fold a stream's events into current entries, keyed by entry id, in first-seen order."""
    out: dict[str, FoldedEntry] = {}
    for ev in events:
        if ev.id is None:
            continue  # stream-level events (document_replaced and friends) carry no entry identity
        entry = out.get(ev.id)
        if entry is None:
            entry = FoldedEntry(id=ev.id, first_seen=ev.ts)
            out[ev.id] = entry
        entry.last_seen = ev.ts

        if ev.event == EVENT_ENTRY:
            entry.payload.update(ev.payload)
            entry.observations += 1
            # An entry re-observed after a tombstone does not resurrect: only an explicit operator
            # action clears a tombstone, and there is no such event by design.
        elif ev.event == EVENT_SUPERSEDED_BY:
            if entry.status != RETIRED:
                entry.status = SUPERSEDED
            entry.superseded_by = ev.payload.get("superseded_by")
        elif ev.event == EVENT_RETIRED:
            entry.status = RETIRED
            entry.retired_reason = ev.payload.get("reason")
        elif ev.event == EVENT_CONTRADICTED:
            entry.contested = True
        else:
            # Unknown adapter event kinds still count as observations of the entry and may carry
            # payload; the engine stays out of their semantics.
            entry.payload.update(ev.payload)
    return out


def active(entries: dict[str, FoldedEntry]) -> dict[str, FoldedEntry]:
    return {k: v for k, v in entries.items() if v.is_active}


def tombstoned_ids(entries: dict[str, FoldedEntry]) -> set[str]:
    return {k for k, v in entries.items() if v.is_tombstoned}
