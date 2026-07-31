"""Every error MEMENTO raises. Flat hierarchy on purpose — callers branch on kind, not depth."""

from __future__ import annotations


class MementoError(Exception):
    """Base for everything this library raises."""


class CorruptStoreError(MementoError):
    """The store on disk is not readable as a store. Never raised for a torn trailing line."""


class SchemaVersionError(MementoError):
    """The store was written by an engine whose schema this build does not speak."""


class LockTimeout(MementoError):
    """A lock could not be acquired inside its timeout. The caller retries or defers; it never forces."""


class ClaimHeld(MementoError):
    """Another live process holds this session's claim. Not an error condition — a signal to skip."""


class GateFailure(MementoError):
    """A consolidation was rejected by the deterministic write gates. Nothing was written.

    `violations` carries every rule that fired, not just the first: the write is all-or-nothing,
    so the operator gets the whole picture in one FLAG.
    """

    def __init__(self, violations: list["Violation"]) -> None:  # noqa: F821
        self.violations = list(violations)
        super().__init__(self.render())

    def render(self) -> str:
        return "consolidation rejected: " + "; ".join(v.render() for v in self.violations)


class StaleProposal(MementoError):
    """A consolidation derived from facts that another writer has since replaced.

    Not a corruption and not the caller's fault — two front-ends drained concurrently. The write is
    refused so the newer state survives, and the session stays pending for a redrive against it.
    """


class SecretsDetected(MementoError):
    """Content matching a secret pattern tried to enter the store. Fail closed, always."""


class BudgetError(MementoError):
    """A read-path budget could not be honored even after deterministic truncation."""


class BackupError(MementoError):
    """Git backup is misconfigured, not opted into, or the subprocess failed."""


class DrainRefused(MementoError):
    """A drain was asked to spawn outside its gate (prefix not materialized, or session not idle)."""
