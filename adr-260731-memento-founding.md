# ADR — MEMENTO: 42labs Agent Memory/Relationship Engine

**Status:** **SIGNED — all §5 operator decisions settled 2026-07-31** (v7; reviewed FIT at round 6 of six adversarial rounds — 24+10+7+3+8+3 findings, all folded). Phase A = clean-room build in `4242labs/memento` (public). Next: scope Phase A into blocks/cards.
**Date:** 2026-07-31
**Author:** FABLE (operator session)
**Interacts with:** jubs founding ADR **rev 6 §3.5/§3.12** (the live authority on the jubs store — the 260729 datastore-boundary addendum is SUPERSEDED-by-rev-6); jubs Phase 6 block `06-01` (T1.2 quit-consolidation deferral must be implemented the D3 way so Phase B swaps internals without rework).
**Scope:** every 42labs agent/product needing long-term memory of — and a relationship with — its operator/user. First consumer: jubs.
**Operator-settled, not up for review:** one shared **engine** applied per-project; **never shared data**; no central service, no compartments — N fully independent memories run by the same code.

---

## 1. Context

jubs shipped v1 with a working memory whose *disciplines* are the real asset: an **append-only event log with status folded at read time** (`fold.py` — "status is never stored"), **idempotent batch appends + atomic file replacement + marker-last ordering** for crash safety, and — most transferable of all — **deterministic validated writes**: LLM consolidation output is accepted only if it passes schema validation, derived-identity re-checks, and **monotonicity/anti-erosion gates** (no language may disappear, CEFR moves ≤1 step, no interest topic may be dropped — tombstone, never delete). Store: two projected documents (`profile.md`, `interests.md`), per-language JSONL event logs, free-prose session logs, in a plain per-project git repo.

Upcoming 42labs agents need the same capability. Rebuilding per project forks bugs and never accumulates quality.

**Field snapshot (2026, references §7), with honest applicability:** memory isolation-by-scope is the industry norm (Anthropic's project-scoped memory); two-tier memory (small in-prompt core + on-demand archive) is the consensus shape (Letta lineage; the operator-supplied CMA paper argues the same: memory must be written/updated/chained, not just retrieved); consolidation belongs off the interactive path — though "sleep-time compute" presumes an always-on server we don't have, so our version is *deferred, non-blocking drains* (D3), not a daemon; hierarchical/temporal abstraction (TiMem/CMA) is promising but data-hungry — deferred (D9); memory sycophancy is a named failure mode (MemSyco-Bench) — our defense is jubs' deterministic write gates, not trust in the writer; the operator-supplied ATANT paper motivates **LLM-free judging** in the eval harness (D6).

## 2. Decision

A Python library, consumed per-project as a **private git-pinned dependency** (no PyPI/semver ceremony until Phase C; prompt templates pinned by the same SHA). No MCP face in v1 — deferred until a named consumer needs it, because its only plausible near-term use (the operator's coding agent reading a learner store) is a data-boundary crossing the operator must rule on explicitly first.

### D1 — Engine / adapter / store split
| Layer | Lives where | Shared? |
|:--|:--|:--|
| Engine (this ADR) | `4242labs/memento` (public), git-pinned dep | Code only |
| Adapter — taxonomy, **distillation/consolidation prompts**, recall policy, budgets | Inside each consumer app repo | No |
| Store — the data | **A git-ignored `memory/` directory inside the app repo** (operator ruling 2026-07-31): zero additional repos, no nested git. Never *tracked* by the app repo (runtime commits would pollute code history/CI/branch flow — the jubs datastore lesson). **`.gitignore` pattern must be root-anchored — `/memory/` — never `memory/`**, which git matches at any depth and would untrack source packages like `jubs-app/src/jubs/memory/`. The append-only event log (incl. `document_replaced`, D2) is the audit history; crash-safety is journal + marker-LAST, not git. A store gains `git init` + remote **only** on push/backup opt-in (D8). jubs' `jubs-memory` repo is grandfathered until the owned Phase-B move (D9). | **Never** |

**The store IS the namespace.** The interface takes a `store_root` — there is no `(agent, user)` tuple, no multi-tenant seam in the API. Prompt ownership rule: the engine ships **relationship/restraint** templates (SHA-pinned, vendored); the consumer owns **domain distillation** prompts.

### D2 — Storage model: jubs' proven model, generalized as-is
`MemoryStore` interface over exactly the model that exists and works — plain human-readable files (markdown + JSONL), user-owned, in the git-ignored `memory/` directory (D1):
- **Event log** (append-only JSONL per stream): idempotent batch append; every event stamped `ts / session / turn / batch / ordinal` (adds `turn` to today's stamp — the one extension). **The event log (including `document_replaced` events) is the audit history** — git is not required for auditability; a store has git only after its owner opts into push/backup (D8). Honesty note: git-less auditability is *cooperative* (a local file any process could rewrite), not tamper-evident — where tamper-evidence matters, push-enabled stores with chained commit SHAs are the option.
- **Projected documents** (markdown, wholesale-replaced atomically): folded from events + LLM consolidation. **Provenance-exempt as individual entries** — their accountability comes from the `document_replaced` event (author session + prior content), not from derivability: the LLM-authored documents (profile/interests) are **not reconstructible from the event log**, so every projected-document write **appends a `document_replaced` event carrying the prior content** — without this, a bad consolidation that passes the gates would destroy the prior profile irrecoverably on a git-less store. Idempotency key: `(session, batch, document, ordinal)` — a crash re-run after the replace has landed appends **no duplicate** (it would otherwise capture the already-replaced file as "prior"). Inline prior content by default; the hash+pointer variant (adapter-marked large documents) must point into a **retained content-addressed area inside the store**, else rollback is unavailable for that document and the adapter accepts that trade-off explicitly. The event is what makes the audit claim below true for the whole store, and it doubles as the rollback mechanism for operator `edit`/`forget`.
- **Status is never stored — always folded from event history at read time.** Supersession/retirement are *events* (`superseded_by`, `retired`), never in-place mutation. This preserves idempotent crash recovery and keeps the full history in the log itself (with `git log` as a second view where push/backup is enabled).
- Store layout stays byte-compatible with today's `jubs-memory` — **the Phase-B jubs adoption requires zero layout migration** (jubs' store moves *path* to `jubs-app/memory/`, contents unchanged). A `schema_version` marker file is added for future evolution. (One-fact-per-file was considered and **rejected**: thousands of files are worse for `git log`, LLM readability, and the store that exists; it solves granular supersession nobody needs.)
- **The queue is part of the store contract:** the pending-consolidation area (journals, `pending.jsonl`, `consolidated` markers, per-turn STT/reply text — today jubs' `sessions-data/`, *outside* the memory repo and unversioned) is named as the store's second, explicitly **unversioned** area. It is covered by the same D3.3 lock for claim operations, and the claim is a **distinct, ephemeral artifact — explicitly NOT the `consolidated` marker**: a per-session `flock` held for the claiming process's lifetime (OS-released if the holder dies — jubs' existing `session_write_lock` primitive), or equivalently a claim file with PID + timestamp and a staleness TTL that a later drain reclaims. **`consolidated` stays marker-LAST, unchanged** — the crash-safety invariant (crash before marker ⇒ re-run, never lose) is load-bearing and this ADR does not touch it. Orphaned claims are reclaimable by construction; the post-call `is_consolidated` re-check remains as backstop, so two front-ends never both pay for one consolidation.

### D3 — Write path: validated, deferred, locked, committed
1. **Validated write (the anti-sycophancy/anti-hallucination mechanism):** consolidation output is accepted **all-or-nothing** through deterministic gates — schema validation, derived-identity re-checks, and monotonicity/anti-erosion rules. The engine ships a **mandatory, non-disableable floor** (sets shrink only by tombstone; ordered-scale fields move ≤1 step); adapters may *tighten*, never disable — an adapter declaring no rules gets the floor, and the D6 harness proves an empty adapter rule set **fails closed**. Nothing written on failure; failure marks the session `deferred` and FLAGs. Provenance guards nothing by itself (the same LLM writes it) — the gates are the defense.
2. **Deferred + non-blocking, with a named mechanism:** session exit only closes the journal and enqueues (exit ≤5s — the behavior jubs 06-01/T1.2 must implement). The drain runs as a **detached subprocess** (jubs: the existing `jubs consolidate` entrypoint) — *not* a thread: the consolidation abort handler installs POSIX signal handlers, which only work on the main thread, and the operator-visible "Ctrl-C twice to abort, keep the journal" contract (§3.2) survives intact *inside the child*. **§3.2 amendment required:** during a background drain the double-Ctrl-C abort no longer applies to the parent session — this is an operator-visible change to a signed contract and is routed to the operator (open item §5), not decided here. Placement/contention is **spawn-gated by the parent** — a detached subprocess cannot observe parent state and cannot pause a 19s LLM call mid-flight, so the rule is about when to *start*, not suspending: the parent spawns the drain only **after the session's read prefix is fully materialized** (a drain writing `profile.md`/`interests.md`/event logs while the prefix reader concatenates them would produce a torn composite — one-session-*stale* is allowed, *inconsistent* is not) **and after ≥N seconds of session idle**; once spawned, the drain runs to completion — the drain's LLM call shares the hot path's credential, quota, and Apple-Silicon compute, and trading a visible 19s exit for an invisible mid-conversation stall would be a worse bug than the one being fixed. Spawn-gating **reduces overlap, it does not eliminate it** (speech resuming right after spawn still overlaps the call) — the bound is empirical, guarded by 06-01 AC-7's drain-in-flight first-turn p95 measurement. **Session exit performs no git work at all** — journal close + enqueue only; on push-enabled stores, every `add`/`commit`/`pull`/`push` happens in the drain subprocess (which is also what makes D3.4's attribution airtight rather than conventional); on default (git-less) stores there is no git work anywhere. Triggers per front-end shape: CLI — next startup, post-prefix, idle-gated; long-lived server — session start or idle timer. No daemon/cron unless a consumer explicitly opts into a unit the engine ships but never installs by default.
3. **Store-level lock, bounded:** one write lock keyed on `store_root` (not session id), serializing consolidation writes, queue **claim** operations (D2), and — on push-enabled stores — the **local** half of autocommit (`add` + `commit`). Lock ordering: the store lock is held **only** for claim acquisition and for the write/commit — **never across the LLM call** (take store lock → acquire per-session claim → release store lock → LLM call → retake for write/commit). The lock is **released before `pull`/`push`** — network I/O never runs under it (a hung push must not stall the other front-end) — and all git subprocess calls carry explicit timeouts; maximum lock hold is local-I/O-bounded by construction. Must land before jubs 06-01/T2 goes live.
4. **Commit attribution (push-enabled stores only):** each drained consolidation is committed immediately, message carrying the **consolidated** session's id (never batched under a later session). On default git-less stores there is no commit; attribution lives in the event stamps and `document_replaced` events (D2).
5. **Backlog bound:** pending consolidations FLAG at N pending or M days stale, surfaced at session start ("memory is stale by K sessions") — deferral must never become silent rot.
6. **LLM-unavailable:** drain failures are caught, marked `deferred`, counted toward the backlog bound. Bounded, visible, never fatal.

### D4 — Read path: two tiers, actually budgeted
(a) **Core prefix** — profile / preferences / goals / active threads, assembled by the engine under a **token budget it enforces**. Counting uses a **named counter** — on the hot read path (prefix assembly at session start, inside the startup window 06-01 is optimizing) a **pinned local tokenizer** declared by the adapter, never a network call; the serving provider's token-count API is for offline validation and the D6 harness (a cache prefix must be counted in the serving model's units; "adapter supplies a number" is not a mechanism). On overflow: **deterministic truncation in an adapter-declared priority order — never silent overflow**. Today's unbounded concatenation is a defect the engine fixes (jubs inherits the fix at Phase B), since prefix growth silently erodes prompt-cache economics. (b) **Selective recall** — search over events + documents on demand. The archive is never bulk-loaded.

### D5 — Forgetting: tombstone, never delete
Retirement is an *event* (D2); tombstoned entries remain visible to the fold (jubs' anti-erosion gate requires it). Reconsolidation-on-retrieval: an entry surfaced and contradicted live gets a correcting event next consolidation. Operator `forget` (D8-style verbs: view/edit/forget via CLI; plain files + the append-only log are the audit trail) writes a tombstone honored by all future folds and consolidations. TTL-decay of *nominations* (what gets surfaced) is allowed; TTL-*deletion* of data is not.

### D6 — Evaluation: deterministic tier gates, live tier informs (ATANT-derived)
- **Deterministic tier (the CI gate):** the distiller is **stubbed with recorded fixtures** — no live model, no credentials, no token spend. Tests everything downstream of the LLM: gates (schema/identity/anti-erosion, **including: an empty adapter rule set fails closed on the floor rules**), dedupe/supersession, tombstoning, `document_replaced` history + rollback (**incl.: re-run after replace appends no duplicate `document_replaced`**), idempotent crash recovery (kill between append and replace; **kill between claim and write — session must remain pending; orphaned claim reclaimed by a later drain**), drain triggers + lock contention (two front-ends, no lost write, no double-paid consolidation), commit attribution (**push-enabled fixture store**), budgeted-prefix truncation, and **probe recall with zero cross-topic contamination** as the store grows. Namespace isolation (wrong-store impossible) is a unit test, not an eval axis.
- **Live tier (non-gating):** sampled end-to-end runs with tolerance assertions, run on demand.
- **No real operator data in any corpus.** This split follows jubs' own discipline: the LLM call is injected precisely so write discipline is testable without a model.

### D7 — Deferred (explicitly not in v1)
- **Abstraction pass** (TiMem/CMA higher-order patterns): unfalsifiable on stores with a handful of sessions — Phase C, when a store is big enough to abstract.
- **Spaced-repetition scheduler:** a jubs *product* feature — ships in jubs' adapter as an additive `next_review` nomination **alongside** the existing recency watchlist (changing watchlist semantics would amend rev 6 §3.5 — not this ADR's call). Generalized into the engine only if consumer #2 wants it.
- **MCP face, vector/graph backends, multi-user anything:** behind the interface; each needs a named consumer + (for MCP/multi-user) an operator data-boundary ruling.

### D8 — Data handling (store AND queue — the personal-data pile is bigger than the store)
- **Distilled-by-default in the store:** what the memory store persists is distilled entries + the free-prose session log (retention/verbosity is a stated adapter policy). But the real verbatim pile lives in the **queue/session-data area** (per-turn STT JSON, reply text, archived WAVs — unbounded and TTL-less in jubs today, and 06-01/T2 will serve those WAVs over HTTP): D8 covers it explicitly. The engine may **prune a session's transcript material once that session is consolidated**, per an adapter retention policy the consumer must state (jubs' policy — **keep everything** — settled by the operator 2026-07-31, §5); "not persisted in the memory store" is never to be read as "transient".
- **Remote push is opt-in** per store, with an explicit warning at configuration time. The existing `jubs-memory` remote *already pushes unattended*; it is grandfathered only until the Phase-A move (D9), after which jubs' push is a fresh explicit opt-in and the old repo's fate is the operator's §5 decision — no silent default either way.
- Secrets never enter the store (gate rejects entries matching secret patterns).

### D9 — Adoption path *(operator decision 2026-07-31: clean-room build, NOT extraction)*
- **Phase A — build MEMENTO from scratch** against this ADR (D1–D8 incl. the D6 harness) in `4242labs/memento`. jubs' `src/jubs/memory/` is **not** the seed — it serves only as a behavioral reference, and its store layout is the compatibility target (D2's zero-layout-migration guarantee stands so jubs adopts without converting data).
- **Phase B — jubs adopts:** thin adapter replaces jubs' memory internals with the engine; **owned move step:** relocate the store `jubs-memory` → `jubs-app/memory/` (git-ignored, root-anchored pattern), contents unchanged; enable backup to a **fresh private remote** (operator opt-in, 2026-07-31); then **delete** the old local repo **and** the `42piratas/jubs-memory` GitHub repo (operator decision, 2026-07-31 — pre-move `profile.md`/`interests.md` version history knowingly discarded; the new store carries `document_replaced` history from day one).
- **Phase C:** second consumer proves the adapter boundary → API called stable; only then consider deferred D7 items.

## 3. Alternatives rejected

- **Mem0 / Zep / Letta wholesale** — hosted/heavier, data custody outside user-owned plain files, vector/graph-first for scale we don't have. D2's interface stays shaped so one could be mounted as a backend later.
- **Central service with compartments** — operator-rejected; isolation-by-architecture beats isolation-by-policy.
- **Vector store in v1** — retrieval at single-operator scale is grep/structured search; embeddings add infra + opacity, no measured win.
- **One-fact-per-file entry model** — rejected (D2).
- **Per-project bespoke memory (status quo)** — quality never accumulates.

## 4. Consequences

- **BL-05 / 42L-1208, honestly:** D2 absorbs its *storage-interface* half. Its *multi-user/managed-backend* half (real users won't maintain git repos; auth/tenancy) is **out of scope for this engine** and remains a separate product decision — the card must say so.
- jubs 06-01 coupling: T1.2 (exit deferral) and T2 (second front-end) must implement D3.2–D3.4 (subprocess drain, store lock, conditional commit attribution) so jubs' behavior already matches the engine contract when Phase B swaps the internals — block amended accordingly. The §3.2 abort amendment is **deliberately undecided until the 06-02 sitting** (operator, 2026-07-31 — tried live first).
- One new repo to maintain: engine + deterministic harness as its own regression net; consumers pin by SHA.
- Risks: over-abstraction before consumer #2 (gated by Phase C); consolidation drift (defended by D3.1 gates + D6); deferral rot (defended by D3.5 bounds + FLAGs).

## 5. Operator decisions — ALL SETTLED (2026-07-31)

- [x] System name — **MEMENTO**
- [x] Engine repo home + visibility — **`4242labs/memento`, public** (safe by design: engine code + synthetic fixtures only, secrets scan gated in CI)
- [x] Store placement — **git-ignored `memory/` directory inside each app repo; zero additional repos; git only on push opt-in**
- [x] Session-data retention — **keep everything** (archives are SA-10 artifacts + feed sound cards)
- [x] Phase A — **approved as CLEAN-ROOM build** (jubs code is reference only, not the seed; D9 rewritten accordingly)
- [x] §3.2 abort-contract amendment — **deferred to the 06-02 sitting** (operator tries the new quit behavior live; stays in the 06-02 Operator Inbox)
- [x] jubs backup after the Phase-B move — **opt-in, fresh private remote**
- [x] Old `42piratas/jubs-memory` GitHub repo — **DELETE** at the move (pre-move version history knowingly discarded)

**ADR SIGNED by these decisions — next step: scope Phase A (clean-room engine build) into blocks/cards.**

### Amendment A1 — store directory name (operator, 2026-08-02)

**Signed off by the operator on 2026-08-02: "yes, standardize as `memento`".** This amends the
*Store placement* decision above, which was otherwise settled at signature; nothing else in §5 moves.

- The default store directory is **`memento/`**, not `memory/`. Everything else about the decision
  stands: git-ignored, inside each app repo, zero additional repos, git only on push opt-in.
- **Root-anchoring is the load-bearing half, not the name.** The pattern is `/memento/` — anchored —
  because a bare `memento/` would untrack a source package named `memento` at any depth, including
  this engine's own `src/memento/`. That is the same trap `/memory/` was anchored against in review
  round 5; renaming without anchoring would reintroduce it in a worse place.
- **Nothing on disk has to move.** `store_root` is a path the consumer passes; the engine has never
  hard-coded either name. `/memory/` stays in this repo's `.gitignore` for stores that predate the
  rename.
- jubs' own move (`jubs-app/memory/` → `jubs-app/memento/`) is **separately operator-gated** and is
  not covered by this amendment.

## 6. Review record

**Rounds 5–6 (2026-07-31, same reviewer):** round 5, on the v5 store-placement fold — 5 MAJOR / 3 minor (projected-document history via `document_replaced`, owned `jubs-memory` retirement + remote fate, conditional-git everywhere incl. 06-01 `dbc722d`, `/memory/` root-anchoring, cooperative-auditability note) → v6. Round 6, on v6 — 8/8 resolved, **VERDICT: FIT for operator signature**; 3 spec-detail minors folded immediately (large-doc pointer must resolve to a retained content-addressed area or rollback is knowingly unavailable; provenance-exemption rationale restated via `document_replaced` accountability; idempotency key `(session, batch, document, ordinal)` + no-duplicate-on-re-run test).

**Round 4 (2026-07-31, same reviewer, on v4): VERDICT — FIT for operator signature.** 7/7 round-3 findings confirmed substantively resolved; 3 wording-level minors (no correctness risk), folded immediately: lock-ordering clause (store lock never held across the LLM call), "claim/markers" → "claim operations", spawn-gating residual stated honestly (overlap reduced not eliminated; AC-7 is the empirical guard).

**Round 3 (2026-07-31, same reviewer, on v3):** 10/10 round-2 residuals confirmed substantively resolved; 1 CRITICAL / 3 MAJOR / 3 minor NEW, all in the claim/drain protocol the round-2 fixes introduced — folded into this v4: claim is an ephemeral `flock`/PID+TTL artifact, **never** the `consolidated` marker (marker-LAST invariant untouched; orphaned claims reclaimable; both kill-windows added to D6); contention converted from unimplementable suspend-during to **parent spawn-gating** (post-prefix + idle, run-to-completion); **exit performs no git work at all** (all git in the drain); D8 retention item now names the sound-card conflict; hot-path token counting is local-tokenizer-only; the "lifts rather than rewrites" claim honestly scoped.

**Round 2 (2026-07-31, same reviewer, on v2):** 22/24 round-1 findings confirmed resolved; 1 CRITICAL / 6 MAJOR / 3 minor residuals, all in the concurrency window D3 created — folded into this v3: drain mechanism named (detached subprocess; thread impossible — signal handlers are main-thread-only) + §3.2 abort amendment routed to operator; "concurrent with warm-up" struck (torn-composite read) — prefix materializes before any drain write; in-flight-turn suspension rule (no invisible mid-conversation stall); queue (`sessions-data`) named part of the store contract with claim-under-lock (no double-paid LLM call); D8 extended to the queue + retention decision; gate floor made engine-mandatory with fail-closed test; lock releases before `pull`/`push` + git timeouts; tokenizer + overflow behavior named in D4; 06-01 AC added for the binding clauses; §5 open items completed.

Adversarial review round 1 (2026-07-31, Opus, dispatched by FABLE): 5 CRITICAL / 13 MAJOR / 6 minor+overengineering — all 24 folded into v2. Material reversals vs v1: entry model (one-fact-per-file → jubs' event+projection model, zero migration), mutation discipline (edit-in-place → append events + folded status), anti-sycophancy mechanism (provenance → deterministic validated-write gates), D9-was-eval now split deterministic-gating/live-informing, abstraction pass + scheduler + MCP face deferred out of v1, store-level lock + data-handling policy + backlog bounds + commit attribution added, BL-05 claim corrected, citations narrowed (RecMem already satisfied; sleep-time-compute daemon premise doesn't apply; Anthropic file-tool lacks our gate discipline).

## 7. References

- Operator-supplied: [ATANT (arXiv 2604.06710)](https://arxiv.org/abs/2604.06710) · [Continuum Memory Architectures (arXiv 2601.09913)](https://arxiv.org/abs/2601.09913)
- Consolidation: [sleep-time compute / AutoDream](https://kenhuangus.substack.com/p/why-ai-agents-are-starting-to-dream) (server-premise caveat, §1) · [RecMem (arXiv 2605.16045)](https://arxiv.org/pdf/2605.16045) · [TiMem (arXiv 2601.02845)](https://arxiv.org/pdf/2601.02845) · [Human-inspired memory (arXiv 2605.08538)](https://arxiv.org/html/2605.08538)
- Landscape: [Mem0/Zep/Graphiti/Letta/LangMem compared](https://medium.com/@wasowski.jarek/i-compared-5-ai-agent-memory-systems-across-6-dimensions-none-wins-6a658335ed0a) · [Mem0 state-of-memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026) · [Mem0 vs Zep vs Letta](https://www.agenticwire.news/article/mem0-zep-letta-agent-memory) · [Atlan ranking 2026](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/)
- Failure modes: [MemSyco-Bench (arXiv 2607.01071)](https://arxiv.org/pdf/2607.01071)
- Isolation precedent: Anthropic Claude memory (project-scoped, strictly isolated)
- Internal: jubs founding ADR rev 6 §3.5/§3.12; jubs `memory/` (`fold.py`, `events.py`, `consolidation.py`, `readpath.py`, `autocommit.py`)
