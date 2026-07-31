"""Token counting for the read path (ADR D4).

The prefix budget is only real if something counts it. On the hot path — prefix assembly at session
start — counting uses a **pinned local tokenizer the adapter declares**, never a network call: a
cache prefix has to be counted in the serving model's units, and "the adapter supplies a number" is
not a mechanism.

The serving provider's token-count API is for offline validation and the harness. `is_local` is how
the engine tells the two apart, and `assemble_prefix` refuses a non-local counter outright.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenCounter(Protocol):
    name: str
    is_local: bool

    def count(self, text: str) -> int: ...


class HeuristicCounter:
    """A deterministic local counter with no model dependency.

    Words plus punctuation runs, which tracks BPE closely enough to budget against and — more
    importantly — gives the harness a counter that cannot drift between runs. A consumer with a real
    tokenizer declares that one instead; this is the floor, not the recommendation.
    """

    name = "memento.heuristic.v1"
    is_local = True

    _TOKEN = re.compile(r"\w+|[^\w\s]")

    def count(self, text: str) -> int:
        if not text:
            return 0
        return len(self._TOKEN.findall(text))


DEFAULT_COUNTER: TokenCounter = HeuristicCounter()
