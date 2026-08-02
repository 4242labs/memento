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
from .flags import PREFIX_TRUNCATED, RECALL_TRUNCATED, Flag, FlagSink
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
        # parts — and by an amount no per-section allowance can predict. So trim one line at a time
        # off the least important end and re-measure the whole, which is the only number that is a
        # promise. Computing an allowance instead meant any section that fit *alone* came back
        # untrimmed and was dropped wholesale: a one-token overflow cost an entire section.
        last = next((r for r in reversed(results) if r.tokens and not r.dropped), None)
        lines = parts[-1].split("\n")
        if last is not None and len(lines) > 1:
            parts[-1] = "\n".join(lines[:-1]).rstrip()
            last.truncated = True
        else:
            parts.pop()
            if last is not None:
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
    ts: str | None = None  # last-seen timestamp, for event hits; documents carry none

    def render(self) -> str:
        where = f"{self.location}:{self.entry_id}" if self.entry_id else self.location
        return f"[{self.score}] {where}  {self.text}"


@dataclass(eq=False)
class RecallResult:
    """Hits, plus what it cost and what was left out. Truncation is never silent.

    Deliberately still usable as the plain list `recall` used to return — iterated, indexed, tested
    for emptiness, compared against a list of hits. The accounting is an addition to that contract,
    not a replacement for it: a caller who only wants the hits should not have to learn a new shape
    because a budget parameter exists.
    """

    hits: list[RecallHit] = field(default_factory=list)
    tokens: int = 0
    budget: int | None = None
    counter: str | None = None
    dropped: int = 0
    flags: list[Flag] = field(default_factory=list)

    @property
    def truncated(self) -> bool:
        return self.dropped > 0

    def __iter__(self):
        return iter(self.hits)

    def __len__(self) -> int:
        return len(self.hits)

    def __getitem__(self, index):
        return self.hits[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, RecallResult):
            return self.hits == other.hits and self.dropped == other.dropped
        return self.hits == other


def _terms(query: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(query)]


def _score(text: str, terms: Sequence[str]) -> int:
    haystack = set(t.lower() for t in _WORD.findall(text))
    return sum(1 for t in set(terms) if t in haystack)


def _within(ts: str | None, since: str | None, until: str | None) -> bool:
    """Date-range filter on ISO-8601 timestamps, compared as text.

    The store writes `YYYY-MM-DDTHH:MM:SSZ`, which sorts lexicographically in time order, so a
    string compare is the whole comparison — no parsing, no timezone arithmetic, nothing to get
    wrong. An entry with no timestamp is outside every range rather than inside all of them.
    """
    if since is None and until is None:
        return True
    if ts is None:
        return False
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True


def recall(
    store: MemoryStore,
    query: str,
    *,
    limit: int | None = None,
    streams: Iterable[str] | None = None,
    documents: Iterable[str] | None = None,
    include_retired: bool = False,
    keys: Iterable[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    budget: int | None = None,
    counter: Any = None,
    sink: FlagSink | None = None,
) -> RecallResult:
    """Selective recall over events and documents.

    A hit must share at least one term with the query. That is what keeps a probe for one topic from
    dragging in a neighbouring one as the store grows — the archive is searched, never surveyed.

    Narrowed two ways, and they are different questions. **Filters** — `streams`, `documents`,
    `keys`, `since`/`until` — say what is eligible at all. **`budget`** says what the answer may
    cost once assembled: an agent pastes recall output into its own context, so a bound on the
    number of hits is not a bound on the tokens they carry. Both are deterministic, and dropping
    for budget is reported rather than silent.
    """
    sink = sink if sink is not None else FlagSink()
    terms = _terms(query)
    if not terms:
        return RecallResult(budget=budget)

    wanted_keys = set(keys) if keys is not None else None
    hits: list[RecallHit] = []
    stream_ids = list(streams) if streams is not None else [
        s for s in store.streams() if not s.startswith(".memento/")
    ]
    for stream in stream_ids:
        for entry_id, entry in fold(store.log(stream).read()).items():
            if not include_retired and entry.status != ACTIVE:
                continue
            if wanted_keys is not None and entry_id not in wanted_keys:
                continue
            if not _within(entry.last_seen, since, until):
                continue
            text = " ".join(f"{k}: {v}" for k, v in entry.payload.items() if isinstance(v, (str, int, float)))
            blob = f"{entry_id} {text}"
            score = _score(blob, terms)
            if score:
                hits.append(
                    RecallHit(
                        source="event",
                        location=stream,
                        entry_id=entry_id,
                        text=text,
                        score=score,
                        ts=entry.last_seen,
                    )
                )

    doc_names = list(documents) if documents is not None else store.documents()
    if since is not None or until is not None:
        # A projected document is the *current* state, carrying no per-line history. Including it in
        # a time-ranged answer would date it to whenever the reader happened to look.
        doc_names = []
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
    if limit is not None:
        hits = hits[:limit]

    if budget is None:
        return RecallResult(hits=hits, budget=None, flags=list(sink.flags))
    if counter is None:
        raise BudgetError("a recall budget needs a token counter; pass the adapter's")
    if not getattr(counter, "is_local", False):
        raise BudgetError(
            f"token counter {getattr(counter, 'name', counter)!r} is not local; counting a recall "
            "answer may not make a network call"
        )

    kept: list[RecallHit] = []
    used = 0
    for hit in hits:
        cost = counter.count(hit.render())
        if used + cost > budget:
            break  # ordered by score, so the cut falls on the least relevant end, always
        kept.append(hit)
        used += cost
    dropped = len(hits) - len(kept)
    if dropped:
        sink.raise_flag(
            RECALL_TRUNCATED,
            f"recall hit its {budget}-token budget; {dropped} of {len(hits)} hit(s) not returned",
        )
    return RecallResult(
        hits=kept,
        tokens=used,
        budget=budget,
        counter=getattr(counter, "name", type(counter).__name__),
        dropped=dropped,
        flags=list(sink.flags),
    )
