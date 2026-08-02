"""Adopting a store the engine did not write (ADR D1, B-01 §T2).

A consumer that already has memory — jubs did — points the engine at it rather than migrating it.
That only works if the engine can read the existing documents back into facts, because the
anti-erosion floor judges the first consolidation against *something*, and an empty baseline cannot
be eroded.

The contract is one line: `render_documents(facts_from_store(store))` must reproduce the bytes
already on disk. When it does, adoption is a no-op and the first consolidation is judged properly.
When it does not, **the bytes win**: the parse is not trusted, the divergence is FLAGged, and the
adapter defers. Re-projecting an operator's own memory to match a renderer is the operator's call to
make, never a consolidation's — and never a silent one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .flags import ADOPTION_DIVERGED, Flag, FlagSink
from .store import MemoryStore


@dataclass(frozen=True)
class AdoptionReport:
    """What reading a pre-existing store back gave, and whether it can be trusted."""

    facts: dict[str, Any] = field(default_factory=dict)
    diverged: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    flags: tuple[Flag, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.diverged

    def message(self) -> str | None:
        if self.ok:
            return None
        return (
            "these documents do not survive a parse/render round-trip: "
            f"{', '.join(self.diverged)}. The store's bytes are authoritative and were left "
            "untouched; adopting them would rewrite the operator's own memory"
        )


def check_adoption(
    store: MemoryStore, adapter: Any, *, sink: FlagSink | None = None
) -> AdoptionReport:
    """Parse the store's documents back to facts and prove the round-trip, byte for byte.

    Nothing here writes. A divergent document is reported and left exactly as it is — the whole
    point is that the engine notices it cannot reproduce a file *before* something replaces it.
    """
    sink = sink if sink is not None else FlagSink()
    parser = getattr(adapter, "facts_from_store", None)
    if parser is None:
        return AdoptionReport()

    facts = dict(parser(store))
    rendered = dict(adapter.render_documents(facts))

    # Everything the renderer produced, plus every document the adapter *claims* to project that is
    # already on disk. The second half is the load-bearing one: a document the renderer produces
    # nothing for is the sharpest divergence there is — re-projection would blank a file the
    # operator's memory is actually in — and comparing only what was rendered never sees it.
    declared = set(getattr(adapter, "projected_documents", ()) or ())
    names = set(rendered) | (declared & set(store.documents()))

    diverged: list[str] = []
    missing: list[str] = []
    for name in sorted(names):
        on_disk = store.read_document(name)
        if on_disk is None:
            # The renderer produces a document the store does not have yet. That is ordinary on an
            # empty or partial store, and it is not a divergence: nothing is being overwritten.
            missing.append(name)
            continue
        if rendered.get(name) != on_disk:
            diverged.append(name)

    report = AdoptionReport(
        facts=facts,
        diverged=tuple(diverged),
        missing=tuple(missing),
    )
    if not report.ok:
        sink.raise_flag(ADOPTION_DIVERGED, report.message() or "")
    return AdoptionReport(
        facts=report.facts,
        diverged=report.diverged,
        missing=report.missing,
        flags=tuple(sink.flags),
    )
