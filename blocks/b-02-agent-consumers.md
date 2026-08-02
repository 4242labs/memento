# Block B-02: MEMENTO for agent consumers — full write path, declared rules, recall

**Status:** 📋 To do
**Repo:** `4242labs/memento` (+ ADR amendment; jubs follows via pin bump + 42L-1236-style owned step for the rename)
**Depends on:** A-01, B-01 (shipped). PR **memento#8** (declarative adapters + CLI `prefix`/`consolidate`/`facts`) is the foundation — review and land it first, then build on it.
**Created:** 2026-08-02 · **Adversarial pre-dispatch review:** Sonnet, 2026-08-02 — 3 CRITICAL / 4 MAJOR / 4 minor, ALL folded below (claim redesign, backup verb, spawn-gating, T5/T6 rescope, T7 double-gate, race-probe ACs).
**Authority:** founding ADR (SIGNED) + operator orders 2026-08-02: MEMENTO must fully work with regular agents (e.g. `tortuga/agents/advisor-legal` — markdown + a shell, no Python); close the paper gaps (agent-initiative recall). Phase-C "API stable" declaration stays **HELD** (operator 2026-08-02) — everything here ships under provisional API + SHA pins.

**Required reading:** `docs/handoff-a-01.md` §5 · `docs/adapter-contract.md` · `src/memento/drain.py` (DrainGate) + `locking.py` (SessionClaim is process-lifetime-scoped — the reason T2 is a design task) · PR #8 body (the shell-boundary CAS contract: `--expect`/`--unchecked`, exit codes 3/4/5).

---

## Tasks

### T1: Land PR #8
Adversarial review, then merge. It is the base every task below extends. The `uv.lock` tracking change stays in (lockfile belongs on main).

### T2: CLI queue + session-exit path *(gap 1 — claim is a DESIGN task, not a wrapper)*
Agents can't journal or enqueue — no session-exit contract outside Python. Verbs over `queue.py`: `journal` (append turn material), `enqueue` (close + mark pending), `pending` (list + staleness — the D3.5 backlog FLAG surface — **and the drain-gate check**: refuses while idle/prefix-materialized preconditions fail, mirroring `DrainGate.refusal()`).

**Claim cannot be a thin wrapper over `SessionClaim`** — its flock dies with the acquiring process, so a bare `claim` verb releases on exit and excludes nothing (review CRITICAL-1). Ship one of: (a) `memento with-claim <session> -- <cmd>` — the verb holds the flock while a child command runs; or (b) a claim-**file** CAS scheme (atomic create, PID+ts, staleness TTL, reclaim) that is valid across process boundaries. Either way: claim ≠ marker, marker-LAST untouched, orphans reclaimable, and the D2 invariant "two front-ends never both pay for one consolidation" must hold across two *processes*.

**Backup surface (review CRITICAL-2):** `commit_consolidation`/`push` are drain-only today. Fold commit+push into `consolidate` post-write on push-enabled stores (attribution per D3.4), or add an explicit `memento commit` verb — AC-1 names it either way.

### T3: Agent-as-distiller consolidation *(gap 3 — spawn-gated, never at exit)*
The drain assumes the engine calls a Python `Distiller`. An agent **is** the model — the flow inverts: agent reads `prefix` + pending journal, produces a proposal JSON itself, submits via `consolidate --proposal <file> --expect <fp>`. Gates stay engine-side and non-negotiable — the agent is the writer the gates were built to distrust (MemSyco-Bench posture unchanged).

**Timing is D3.2's, not the agent's (review CRITICAL-3):** session exit stays journal-close + enqueue ONLY — no distillation, no git, nothing slow at exit. The distill-and-submit loop runs later (next session start, or an idle moment) and **must first pass the T2 `pending` gate check** (idle ≥N s + prefix materialized). `docs/agent-consumers.md` documents the loop as: enqueue at exit → later: gate-check → claim → prefix+journal → distill → submit → on exit 3/4/5 do X → commit/push — and marks the gate check MANDATORY. Agent consumers use `--adapter-file`/spec **only**; the code-loading `--adapter module:attr` path is documented out of the agent contract (review minor-11).

### T4: Declared domain rules *(gap 2)*
Spec adapters get only the floor; apps can tighten, agents can't. Add a declarative tighten-only vocabulary to `memento.spec`: ordered scales (≤1 step), required-member lists, enum + **JSON-expressible regex pattern strings** for field constraints — never a `Callable`, spec stays code-free (review minor-9). No loosening: a spec rule that would weaken the floor is a spec error, refused at load (unknown keys stay refused, never ignored). The floor already tombstone-guards set shrink — T4 adds field-level and scale tightening only, nothing that restates the floor (review minor-8).

### T5: Existing-store adoption for declared adapters *(gap 4 — parser design, not plumbing)*
`facts_from_store` is code-only. For declared adapters this needs a **new engine-owned inverse parser**: markdown→facts keyed to the spec's declared `schema`/`entry_schema` types — the declared renderer is lossy (bools → "yes"/"no", scalars stringified), so a generic byte-round-trip is impossible without type-directed parsing (review MAJOR-5). Deliverable: the parser + `facts --from-store` on the CLI. Adoption contract unchanged: `render_documents(facts_from_store(store))` must reproduce bytes or the adapter refuses — **bytes win over re-projection**, divergence FLAGs and defers, never rewrites.

### T6: Recall — the *delta* only *(paper gap; `recall` verb already shipped in A-01)*
`memento recall` exists (`readpath.recall`, commit `f42ffcc`) with deterministic ordering + contamination coverage (review MAJOR-4). This task ships only what's missing for agent-initiative retrieval: **token-budgeted output** (adapter/spec-declared budget + deterministic truncation, not just `--limit` counts) and **structured filters** (stream, key, date-range). No vectors (ADR §3 rejection stands).

### T7: Store dir rename `memory/` → `memento/` *(gap 5 — DOUBLE operator gate)*
Default store dir becomes `memento/`, reducing collision with source packages named `memory/`. Two facts bind (review MAJOR-6): the ADR lists store placement under §5 **"ALL SETTLED — not up for review"**, so the amendment itself **requires explicit operator sign-off before the engine half ships** — not just the jubs half; and the load-bearing trap fix is **root-anchoring**, not the name — the new default pattern is `/memento/`, root-anchored, and an AC asserts it. jubs' move (`jubs-app/memory/` → `jubs-app/memento/`) is a B-01-style owned step, separately operator-gated.

### T8: Mutation coverage on the new code *(gap 6)*
**First establish a pre-block mutmut baseline** for every module this block touches that A-01 never measured (`queue`, `readpath`, `cli`, `spec`, `drain` — only gates/store/events/locking have baselines; review minor-10). Then: no new survivors on any new/changed module before merge. The pre-existing 445 stay 42L-1239.

## Acceptance Criteria

| # | Criterion | Verification |
|:--|:--|:--|
| AC-1 | A non-Python agent completes the full loop through CLI alone: journal → enqueue → gate-check → claim → prefix → proposal → gated consolidate → commit/push (the T2 backup surface) | `test (subprocess-only harness)` |
| AC-1b | **Claim race probe:** two concurrent processes contend for the same session — exactly one claims, the loser gets a clean refusal; probe verified to FAIL against a naive process-scoped claim before the fix (handoff §5.1 discipline) | `test` |
| AC-2 | Spec-declared domain rules enforce (tighten) and cannot loosen the floor; loosening spec refused at load | `test` |
| AC-3 | Declared adapter adopts the jubs-layout fixture store via the T5 parser; bytes-win on divergence | `test` |
| AC-4 | `recall` respects a declared token budget with deterministic truncation + structured filters — asserted on the NEW parameters, not re-testing shipped behavior | `test` |
| AC-5 | Rename: operator sign-off recorded for the ADR amendment itself; new default pattern asserted root-anchored `/memento/`; jubs half separately operator-gated | `review + test + cmd:git log` |
| AC-6 | Suite green; pre-block mutmut baseline recorded, then no new survivors on new/changed modules | `ci + cmd:mutmut` |
| AC-7 | `docs/agent-consumers.md` documents the full agent contract: loop order, mandatory gate check, exit codes, CAS, claim scheme + TTL, `--adapter-file`-only | `review` |

## Out of Scope
Hierarchical/temporal abstraction (Phase C, data-hungry — ADR D7) · vectors/embeddings · MCP face (needs operator data-boundary ruling, ADR §2) · the 445 pre-existing survivors (42L-1239) · shim retirement (42L-1255) · queue-material coupling fix (42L-1257 — engine-side, may ride along only if T2 touches `versioned_paths()`) · declaring the API stable (operator HOLD).
