"""Operator-visible FLAGs.

Deferral must never become silent rot (ADR D3.5), so every path that swallows a failure raises a
FLAG instead of a log line. The engine collects them; the consumer decides how the operator hears
about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

GATE_REJECTED = "gate-rejected"
SECRETS_REJECTED = "secrets-rejected"
LLM_UNAVAILABLE = "llm-unavailable"
BACKUP_FAILED = "backup-failed"
STALE_PROPOSAL = "stale-proposal"
WRITE_FAILED = "write-failed"
BACKLOG = "backlog"
STALE_CLAIM = "stale-claim"
PREFIX_TRUNCATED = "prefix-truncated"
RECALL_TRUNCATED = "recall-truncated"
ADOPTION_DIVERGED = "adoption-diverged"
ROLLBACK_UNAVAILABLE = "rollback-unavailable"


@dataclass(frozen=True)
class Flag:
    kind: str
    message: str
    session: str | None = None

    def render(self) -> str:
        where = f" [{self.session}]" if self.session else ""
        return f"{self.kind}{where}: {self.message}"


@dataclass
class FlagSink:
    """Collects flags and optionally forwards each one as it arrives."""

    forward: Callable[[Flag], None] | None = None
    flags: list[Flag] = field(default_factory=list)

    def raise_flag(self, kind: str, message: str, *, session: str | None = None) -> Flag:
        flag = Flag(kind=kind, message=message, session=session)
        self.flags.append(flag)
        if self.forward is not None:
            self.forward(flag)
        return flag

    def __bool__(self) -> bool:
        """Truthy only when it has flags — so callers write `if sink:` to mean "anything to report".

        Which makes `sink or FlagSink()` a trap: an empty caller-supplied sink is falsy and would be
        silently swapped for a fresh one, losing every flag the caller came to collect. Call sites
        use `sink if sink is not None else FlagSink()`.
        """
        return bool(self.flags)
