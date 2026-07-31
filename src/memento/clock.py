"""Time, injected.

Every timestamp and TTL in the engine comes through a Clock so the deterministic harness can
run crash windows and staleness reclaim without sleeping.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Monotonic-ish wall seconds, used for TTLs."""

    def now_iso(self) -> str:
        """UTC ISO-8601, used for event stamps."""


class SystemClock:
    def now(self) -> float:
        return time.time()

    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class FrozenClock:
    """Test clock. Advances only when told to."""

    def __init__(self, start: float = 1_785_000_000.0) -> None:
        self._t = start

    def advance(self, seconds: float) -> None:
        self._t += seconds

    def now(self) -> float:
        return self._t

    def now_iso(self) -> str:
        return (
            datetime.fromtimestamp(self._t, timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )


DEFAULT_CLOCK: Clock = SystemClock()
