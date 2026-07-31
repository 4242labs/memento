"""The read path (ADR D4) — two tiers, actually budgeted.

(a) **Core prefix**: always-loaded, assembled under a token budget the engine enforces with a pinned
local counter. On overflow the engine truncates in the adapter's declared priority order and says
so. Unbounded concatenation is the defect this replaces — prefix growth silently erodes prompt-cache
economics, and "silently" is the part that makes it a bug rather than a cost.

(b) **Selective recall**: search over events and documents on demand. The archive is never
bulk-loaded, and a hit needs an actual term match — a query that matches nothing returns nothing
rather than the nearest thing lying around.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .errors import BudgetError
from .flags import PREFIX_TRUNCATED, Flag, FlagSink
from .fold import ACTIVE, fold
from .store import MemoryStore

SEPARATOR = "\n\n"
_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass
class SectionResult:
    name: str
    priority: int
    tokens: int
    required: bool = False
    truncated: bool = False
    dropped: bool = False


@dataclass
class PrefixResult:
    text: str
    tokens: int
    budget: int
    counter: str
    sections: list[SectionResult] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)

    @property
    def truncated(self) -> list[str]:
        return [s.name for s in self.sections if s.truncated]

    @property
    def dropped(self) -> list[str]:
        return [s.name for s in self.sections if s.dropped]


def assemble_prefix(
    store: MemoryStore,
    adapter: Any,
    *,
    budget: int | None = None,
    sink: FlagSink | None = None,
) -> PrefixResult:
    counter = adapter.token_counter
    if not getattr(counter, "is_local", False):
        raise BudgetError(
            f"token counter {getattr(counter, 'name', counter)!r} is not local; the hot read path "
            "may not make a network call to count a prefix"
        )
    budget = adapter.prefix_budget_tokens if budget is None else budget
    sink = sink if sink is not None else FlagSink()

    ordered = sorted(adapter.prefix_sections, key=lambda s: (s.priority, s.name))
    parts: list[str] = []
    results: list[SectionResult] = []
    used = 0

    for section in ordered:
        text = (section.render(store) or "").strip()
        result = SectionResult(
            name=section.name, priority=section.priority, tokens=0, required=section.required
        )
        if not text:
            results.append(result)
            continue

        sep_cost = counter.count(SEPARATOR) if parts else 0
        cost = counter.count(text) + sep_cost
        if used + cost <= budget:
            parts.append(text)
            used += cost
            result.tokens = cost
            results.append(result)
            continue

        kept, kept_cost = _truncate_to_fit(text, counter, budget - used - sep_cost)
        if kept:
            parts.append(kept)
            used += kept_cost + sep_cost
            result.tokens = kept_cost + sep_cost
            result.truncated = True
        else:
            result.dropped = True
            if section.required:
                raise BudgetError(
                    f"required prefix section {section.name!r} does not fit in {budget} tokens"
                )
        results.append(result)

    text = SEPARATOR.join(parts)
    total = counter.count(text)
    while total > budget and parts:
        # Per-section accounting assumes the counter is additive across the separator. Real BPE
        # tokenizers merge across boundaries, so the assembled whole can exceed the sum of its
        # parts. Trim from the least important end until the *measured* whole fits — the budget is
        # a promise about what gets sent, not about the arithmetic used to predict it.
        last = next(r for r in reversed(results) if r.tokens and not r.dropped)
        kept, _ = _truncate_to_fit(parts[-1], counter, max(budget - counter.count(SEPARATOR), 0))
        if kept and kept != parts[-1]:
            parts[-1] = kept
            last.truncated = True
        else:
            parts.pop()
            last.tokens = 0
            last.truncated = False
            last.dropped = True
            if last.required:
                raise BudgetError(
                    f"required prefix section {last.name!r} does not fit in {budget} tokens"
                )
        text = SEPARATOR.join(parts)
        total = counter.count(text)

    cut = [r.name for r in results if r.truncated or r.dropped]
    if cut:
        sink.raise_flag(
            PREFIX_TRUNCATED,
            f"prefix hit its {budget}-token budget; trimmed: {', '.join(cut)}",
        )

    return PrefixResult(
        text=text,
        tokens=total,
        budget=budget,
        counter=getattr(counter, "name", type(counter).__name__),
        sections=results,
        flags=list(sink.flags),
    )


def _truncate_to_fit(text: str, counter: Any, allowance: int) -> tuple[str, int]:
    """Drop whole trailing lines until the section fits. Deterministic, and never mid-word."""
    if allowance <= 0:
        return "", 0
    lines = text.split("\n")
    while lines:
        candidate = "\n".join(lines).rstrip()
        cost = counter.count(candidate)
        if candidate and cost <= allowance:
            return candidate, cost
        lines.pop()
    return "", 0


# ------------------------------------------------------------------- recall


@dataclass(frozen=True)
class RecallHit:
    source: str  # "event" | "document"
    location: str  # stream id or document name
    entry_id: str | None
    text: str
    score: int


def _terms(query: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(query)]


def _score(text: str, terms: Sequence[str]) -> int:
    haystack = set(t.lower() for t in _WORD.findall(text))
    return sum(1 for t in set(terms) if t in haystack)


def recall(
    store: MemoryStore,
    query: str,
    *,
    limit: int | None = None,
    streams: Iterable[str] | None = None,
    documents: Iterable[str] | None = None,
    include_retired: bool = False,
) -> list[RecallHit]:
    """Selective recall over events and documents.

    A hit must share at least one term with the query. That is what keeps a probe for one topic from
    dragging in a neighbouring one as the store grows — the archive is searched, never surveyed.
    """
    terms = _terms(query)
    if not terms:
        return []

    hits: list[RecallHit] = []
    stream_ids = list(streams) if streams is not None else [
        s for s in store.streams() if not s.startswith(".memento/")
    ]
    for stream in stream_ids:
        for entry_id, entry in fold(store.log(stream).read()).items():
            if not include_retired and entry.status != ACTIVE:
                continue
            text = " ".join(f"{k}: {v}" for k, v in entry.payload.items() if isinstance(v, (str, int, float)))
            blob = f"{entry_id} {text}"
            score = _score(blob, terms)
            if score:
                hits.append(
                    RecallHit(source="event", location=stream, entry_id=entry_id, text=text, score=score)
                )

    doc_names = list(documents) if documents is not None else store.documents()
    for name in doc_names:
        content = store.read_document(name)
        if not content:
            continue
        for line in content.splitlines():
            if not line.strip():
                continue
            score = _score(line, terms)
            if score:
                hits.append(
                    RecallHit(
                        source="document", location=name, entry_id=None, text=line.strip(), score=score
                    )
                )

    hits.sort(key=lambda h: (-h.score, h.source, h.location, h.entry_id or "", h.text))
    return hits[: (limit if limit is not None else len(hits))]
