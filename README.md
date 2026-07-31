# MEMENTO

> *"Remember Sammy Jankis."* — long-term memory/relationship engine for 42labs agents.

An LLM has anterograde amnesia: total memory loss between sessions. MEMENTO is the annotated Polaroids and tattoos — a shared engine any 42labs product attaches to remember, and build a relationship with, *its* operator/user.

**One engine, N independent memories.** Shared code, never shared data: each consumer project runs its own instance against its own isolated store (a git-ignored `memory/` directory inside that project's app repo). No central service, no compartments.

## Design

The founding ADR — [`adr-260731-memento-founding.md`](./adr-260731-memento-founding.md) — is the authority (SIGNED 2026-07-31; hardened through six adversarial review rounds). Headlines:

- **Store:** plain human-readable files — append-only JSONL event logs + projected markdown documents; status folded at read time, never stored; `document_replaced` events give documents history + rollback; git only on explicit backup opt-in.
- **Write path:** LLM-distilled consolidation accepted **all-or-nothing** through deterministic gates (schema, derived identity, monotonicity/anti-erosion floor — adapters may tighten, never disable). Runs deferred in a spawn-gated detached subprocess — never on the interactive path.
- **Read path:** token-budgeted always-loaded core prefix + selective recall. The archive is never bulk-loaded.
- **Forgetting:** tombstone, never delete; reconsolidation on retrieval; operator `view`/`edit`/`forget` as first-class verbs.
- **Eval:** deterministic continuity harness (no LLM in the judging loop) gates CI; live tier informs only. No real operator data in any corpus, ever.

## Status

Phase A (clean-room engine build) — scoped in [`blocks/a-01-engine.md`](./blocks/a-01-engine.md). Phase B (first consumer: jubs) — [`blocks/b-01-jubs-adoption.md`](./blocks/b-01-jubs-adoption.md). Phase C (second consumer proves the adapter boundary) — unscoped by design.
