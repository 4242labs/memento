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

from dataclasses import dataclass, field, replace
from typing import Any

from .flags import ADOPTION_DIVERGED, Flag, FlagSink
from .store import MemoryStore


@dataclass(frozen=True)
class AdoptionReport:
    """What reading a pre-existing store back gave, and whether it can be trusted."""

    facts: dict[str, Any] = field(default_factory=dict)
    diverged: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    #: Documents the check could not verify at all — the parser recovered nothing to compare them
    #: against. Distinct from `diverged`, which means "compared, and the bytes differ".
    unverified: tuple[str, ...] = ()
    flags: tuple[Flag, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.diverged

    def message(self) -> str | None:
        if self.ok:
            return None
        if self.unverified:
            return (
                "this adapter recovered no facts at all from a store that holds "
                f"{', '.join(self.unverified)}. Adoption cannot be verified, so it is refused: an "
                "empty baseline is one free erosion on the first consolidation. A declared adapter "
                "gets a parser for free; a Python one needs `facts_from_store`"
            )
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
    on_disk = list(store.documents())

    if on_disk and not facts:
        # A parser that recovers nothing from a store that plainly holds something has not been
        # verified — it has been *unable to try*, and every comparison below is then vacuously
        # equal. Reporting that as adoptable hands the anti-erosion floor an empty baseline, which
        # is the one free erosion this module exists to prevent. Unverifiable fails closed, exactly
        # as the floor does.
        return _reported(sink, AdoptionReport(diverged=tuple(on_disk), unverified=tuple(on_disk)))

    # Everything the renderer produced, plus every document the adapter *claims* to project that is
    # already on disk. The second half is the load-bearing one: a document the renderer produces
    # nothing for is the sharpest divergence there is — re-projection would blank a file the
    # operator's memory is actually in — and comparing only what was rendered never sees it.
    rendered = dict(adapter.render_documents(facts))
    declared = set(getattr(adapter, "projected_documents", ()) or ())
    names = set(rendered) | (declared & set(on_disk))

    diverged: list[str] = []
    missing: list[str] = []
    for name in sorted(names):
        content = store.read_document(name)
        if content is None:
            # The renderer produces a document the store does not have yet. That is ordinary on an
            # empty or partial store, and it is not a divergence: nothing is being overwritten.
            missing.append(name)
        elif rendered.get(name) != content:
            diverged.append(name)

    return _reported(
        sink, AdoptionReport(facts=facts, diverged=tuple(diverged), missing=tuple(missing))
    )


def _reported(sink: FlagSink, report: AdoptionReport) -> AdoptionReport:
    """Raise the FLAG a failed report implies, and hand the report back carrying it."""
    if not report.ok:
        sink.raise_flag(ADOPTION_DIVERGED, report.message() or "")
    return replace(report, flags=tuple(sink.flags))
