"""MEMENTO — long-term memory/relationship engine for 42labs agents.

One engine, N independent memories. Shared code, never shared data: each consumer runs its own
instance against its own isolated store. There is no central service and no multi-tenant seam —
`store_root` *is* the namespace.

The short version of the shape:

    store = MemoryStore("./memento")         # plain files, git-ignored by the app repo
    prefix = assemble_prefix(store, adapter) # budgeted, always-loaded core
    hits = recall(store, "kites")            # selective; the archive is never bulk-loaded

    queue.close_and_enqueue(session)         # session exit: nothing else, ever
    spawn_drain(...)                         # later, detached, spawn-gated

    apply_consolidation(store, adapter, proposal, ...)   # all-or-nothing, through the gates

Authority for every decision here is `adr-260731-memento-founding.md` in this repo.
"""

from __future__ import annotations

from .adapter import Adapter, PrefixSection
from .adoption import AdoptionReport, check_adoption
from .backup import BackupConfig, commit_consolidation, enable_backup, is_enabled, push
from .clock import Clock, FrozenClock, SystemClock
from .drain import DrainGate, DrainReport, Distiller, backlog_flag, run_drain, spawn_drain
from .errors import (
    BackupError,
    BudgetError,
    ClaimHeld,
    CorruptStoreError,
    DrainRefused,
    GateFailure,
    LockTimeout,
    MementoError,
    SchemaVersionError,
    SecretsDetected,
    StaleProposal,
)
from .events import Event, EventLog
from .flags import Flag, FlagSink
from .fold import FoldedEntry, fold
from .forgetting import (
    document_revisions,
    forget_fact,
    note_contradiction,
    retire_entry,
    rollback_document,
    tombstone,
)
from .gates import FieldSpec, Proposal, RuleSet, StoreState, Violation
from .locking import CasClaim, CasClaimRecord, SessionClaim, StoreLock
from .queue import Queue, RetentionPolicy
from .readpath import PrefixResult, RecallHit, RecallResult, assemble_prefix, recall
from .spec import adapter_from_spec, facts_from_documents, load_adapter
from .store import SCHEMA_VERSION, DocumentWrite, MemoryStore
from .tokenizer import HeuristicCounter, TokenCounter
from .writepath import (
    UNCHECKED,
    WriteResult,
    apply_consolidation,
    current_state,
    facts_fingerprint,
)

__version__ = "0.1.0"

__all__ = [
    "Adapter",
    "AdoptionReport",
    "BackupConfig",
    "BackupError",
    "BudgetError",
    "ClaimHeld",
    "CasClaim",
    "CasClaimRecord",
    "Clock",
    "CorruptStoreError",
    "Distiller",
    "DocumentWrite",
    "DrainGate",
    "DrainRefused",
    "DrainReport",
    "Event",
    "EventLog",
    "FieldSpec",
    "Flag",
    "FlagSink",
    "FoldedEntry",
    "FrozenClock",
    "GateFailure",
    "HeuristicCounter",
    "LockTimeout",
    "MementoError",
    "MemoryStore",
    "PrefixResult",
    "PrefixSection",
    "Proposal",
    "Queue",
    "RecallHit",
    "RecallResult",
    "RetentionPolicy",
    "RuleSet",
    "SCHEMA_VERSION",
    "UNCHECKED",
    "SchemaVersionError",
    "SecretsDetected",
    "StaleProposal",
    "SessionClaim",
    "StoreLock",
    "StoreState",
    "SystemClock",
    "TokenCounter",
    "Violation",
    "WriteResult",
    "__version__",
    "adapter_from_spec",
    "apply_consolidation",
    "assemble_prefix",
    "check_adoption",
    "backlog_flag",
    "commit_consolidation",
    "current_state",
    "facts_fingerprint",
    "document_revisions",
    "enable_backup",
    "facts_from_documents",
    "fold",
    "forget_fact",
    "is_enabled",
    "load_adapter",
    "note_contradiction",
    "push",
    "recall",
    "retire_entry",
    "rollback_document",
    "run_drain",
    "spawn_drain",
    "tombstone",
]
