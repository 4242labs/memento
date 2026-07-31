# Block A-01: MEMENTO Engine — clean-room build (ADR D1–D8)

**Status:** 📋 To do
**Repo:** `4242labs/memento` (public)
**Depends on:** — (fully autonomous)
**Blocks:** B-01
**Merge approval:** auto on CI-green
**Created:** 2026-07-31
**Authority:** `adr-260731-memento-founding.md` (SIGNED) — where this block and the ADR disagree, the ADR wins.

---

## Context

Clean-room implementation (operator decision 2026-07-31): jubs' `src/jubs/memory/` is **behavioral reference only** — no code is copied from it. jubs' store layout is the compatibility target: the D6 harness must prove a store laid out like today's `jubs-memory` (projected `profile.md`/`interests.md`, per-stream JSONL event logs, free-prose session logs) round-trips with **zero layout migration**.

Python library, `uv`-managed, consumed as a git-pinned dependency. No MCP face, no vector store, no abstraction pass, no scheduler (all explicitly deferred — ADR D7).

## Tasks

### T1: Store core (D1/D2)
`MemoryStore` over `store_root`: append-only JSONL event log (idempotent batch append; stamps `ts/session/turn/batch/ordinal`); projected documents (atomic wholesale replace + `document_replaced` event carrying prior content, idempotency key `(session, batch, document, ordinal)`; hash+pointer variant → retained content-addressed area or rollback explicitly unavailable); status always folded, never stored; supersession/retirement as events; `schema_version` marker; queue area (journals, pending, claim artifacts, `consolidated` marker-LAST) as the store's second, unversioned area.

### T2: Write path (D3)
Validated write: schema + derived-identity + **engine-mandatory monotonicity/anti-erosion floor** (sets shrink only by tombstone; ordered scales move ≤1 step) — adapters tighten, never disable; empty adapter rule set fails closed. All-or-nothing; failure → `deferred` + FLAG. Drain runner as library API: spawn-gated detached subprocess pattern (parent decides when: post-prefix, idle-gated; child runs to completion), per-session claim = ephemeral `flock`/PID+TTL (never the `consolidated` marker; orphans reclaimable), store-root lock (never held across the LLM call; never across network I/O), backlog bound + stale FLAG, LLM-unavailable = bounded visible deferral. Session exit does no git work.

### T3: Read path (D4) + forgetting (D5) + controls
Token-budgeted core prefix (pinned local tokenizer on the hot path; deterministic truncation order; never silent overflow); selective recall over events + documents. Tombstone-never-delete; reconsolidation-on-retrieval; `view`/`edit`/`forget` CLI verbs (forget honored in store + future consolidations, rollback via `document_replaced`).

### T4: Backup opt-in (D8) + data handling
Push/backup module: `git init` + remote created **only** on explicit opt-in with warning; lock covers local `add`+`commit` only, released before `pull`/`push`; git subprocess timeouts. Secrets gate on writes. Prune-after-consolidation available but policy-gated per adapter.

### T5: Deterministic harness (D6) + CI
Stubbed-distiller fixture tier (CI gate): gates incl. fail-closed empty rule set; dedupe/supersession/tombstoning; `document_replaced` history + rollback incl. no-duplicate-on-re-run; crash windows (kill append↔replace, kill claim↔write, orphan reclaim); two-front-end lock contention (no lost write, no double-paid call); conditional commit attribution (push-enabled fixture); budgeted-prefix truncation; probe recall + zero cross-topic contamination at growing store sizes; namespace isolation unit test; jubs-layout round-trip (zero migration). Live tier: non-gating, tolerance-based. CI: tests + gitleaks secrets scan (public repo — no real operator data anywhere, synthetic fixtures only).

### T6: Relationship templates + docs
SHA-pinned relationship/restraint prompt templates (reference past naturally + sparingly; no over-recall; corrections outrank flattery). Adapter contract doc (taxonomy, distillation prompt, recall policy, budgets, retention policy). API reference in README.

## Acceptance Criteria

| # | Criterion | Verification |
|:--|:--|:--|
| AC-1 | Full D6 deterministic tier green in CI, no live model/credentials/tokens involved | `ci` |
| AC-2 | jubs-layout fixture round-trips with zero migration | `test` |
| AC-3 | Empty adapter rule set fails closed; anti-erosion floor unremovable | `test` |
| AC-4 | All crash/claim/lock windows tested (append↔replace, claim↔write, orphan, contention, double-pay) | `test` |
| AC-5 | gitleaks gate green; repo contains zero real operator data | `ci + review` |
| AC-6 | Adapter contract + API documented | `review` |

## Out of Scope
MCP face · vector/graph backends · abstraction pass · spaced-repetition scheduler · multi-user anything · any jubs code changes (B-01).
