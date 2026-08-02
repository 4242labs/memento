# Block B-02: MEMENTO for agent consumers — full write path, declared rules, recall

**Status:** ✅ Done — shipped in PR #8 (`2b5c9de`, T1) + PR #9 (`969ae5b`, T2–T8), CI green. All ACs verified 2026-08-02 (architect closure pass): AC-1/1b `tests/test_agent_loop.py` subprocess-only + fail-first race probe; AC-2 tighten-only spec rules; AC-3 bytes-win adoption; AC-4 budgeted recall; AC-5 ADR Amendment A1 (operator-signed, root-anchored `/memento/`); AC-6 amended T8 gate met per `review/mutation-b02.md` — **the +4 message-string survivors are ACCEPTED as a declared exception** (free-text `error` prose, the exact class R1 ruled non-contractual); AC-7 `docs/agent-consumers.md` @ `--json` + exit codes.
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

### T7: Store dir rename `memory/` → `memento/` *(gap 5 — engine catches up to shipped reality)*
**The jubs half already shipped, by the operator personally** (verification pass 2026-08-02): jubs-app `d79326b` / v1.4.1 / ADR rev 10, operator-authored, store at `jubs-app/memento/` with a root-anchored `/memento/` — that commit **is** the jubs-side sign-off; nothing further is gated there. What remains is the engine half: default store dir becomes `memento/`, and because the memento founding ADR lists store placement under §5 **"ALL SETTLED — not up for review"** (still reading `memory/`), the amendment **requires explicit operator sign-off before it ships** — the jubs precedent argues for it but does not substitute for it. Load-bearing detail unchanged: the trap fix is **root-anchoring**, not the name — the new default pattern is `/memento/` and an AC asserts it.

### T8: Mutation coverage on the new code *(gap 6 — AMENDED 2026-08-02, post-measurement architect ruling; supersedes the original gate)*
Measurement finding accepted: `[tool.mutmut] also_copy` was broken on main (5/19 modules copied — no mutant ever ran); the fix ships with this block. A-01's figures reproduce; the pre-existing survivors stay 42L-1239. The gate is now:

- **R1 — the CLI contract is machine-readable, not prose.** Split `cli.py`: a **command layer** (parse → engine call → exit code + structured result) and a **presentation layer** (all human-facing message rendering, own module). Every agent-contract verb gains `--json`; `docs/agent-consumers.md` rewrites the loop against `--json` + exit codes and marks console prose explicitly non-contractual.
- **R2 — ratchet scope by module boundary, never by mutant-class filter.** Re-baseline after the split, then: **no new survivors** vs the committed pre-block baseline on command layer, `spec`, `readpath`, `adoption`; **ALL exit-code mutants dead regardless of baseline** (agents branch on 3/4/5 — enforced by a parametrized scenario→exit-code contract table, in-process). The presentation module is out of ratchet scope **by architecture** — a documented boundary, not a hand-maintained exclusion list. The 4 no-op mutants: mark equivalent, don't chase.
- **R3 — kill mechanism is in-process.** Mutation tests invoke command functions directly; the AC-1 subprocess harness remains end-to-end acceptance only, never the per-mutant runner (this collapses the per-mutant cost).
- **R4 — cadence is two-tier.** Per-PR: incremental sweep, changed modules only. Full sweep: nightly against the committed survivor baselines (`review/`), down-only ratchet, regressions FLAG + card. That design belongs to **42L-1239** — this ruling is its spec; commit the baselines here, build the nightly there.

## Acceptance Criteria

| # | Criterion | Verification |
|:--|:--|:--|
| AC-1 | A non-Python agent completes the full loop through CLI alone: journal → enqueue → gate-check → claim → prefix → proposal → gated consolidate → commit/push (the T2 backup surface) | `test (subprocess-only harness)` |
| AC-1b | **Claim race probe:** two concurrent processes contend for the same session — exactly one claims, the loser gets a clean refusal; probe verified to FAIL against a naive process-scoped claim before the fix (handoff §5.1 discipline) | `test` |
| AC-2 | Spec-declared domain rules enforce (tighten) and cannot loosen the floor; loosening spec refused at load | `test` |
| AC-3 | Declared adapter adopts the jubs-layout fixture store via the T5 parser; bytes-win on divergence | `test` |
| AC-4 | `recall` respects a declared token budget with deterministic truncation + structured filters — asserted on the NEW parameters, not re-testing shipped behavior | `test` |
| AC-5 | Rename: operator sign-off recorded for the memento ADR §5 amendment; new default pattern asserted root-anchored `/memento/`; jubs half graded ALREADY DONE (operator-authored `d79326b`, v1.4.1 — retroactively satisfies its gate) | `review + test + cmd:git log` |
| AC-6 | *(amended)* Suite green; `also_copy` measurement fix landed; command/presentation split + `--json` shipped; exit-code contract table green (every exit-code mutant dead); no new survivors vs post-split baseline on contract-scope modules; presentation exclusion documented; baselines committed in `review/` | `ci + cmd:mutmut` |
| AC-7 | `docs/agent-consumers.md` documents the full agent contract: loop order, mandatory gate check, exit codes, CAS, claim scheme + TTL, `--adapter-file`-only, **`--json` as the parse surface (prose non-contractual)** | `review` |

## Out of Scope
Hierarchical/temporal abstraction (Phase C, data-hungry — ADR D7) · vectors/embeddings · MCP face (needs operator data-boundary ruling, ADR §2) · the 445 pre-existing survivors (42L-1239) · shim retirement (42L-1255) · queue-material coupling fix (42L-1257 — engine-side, may ride along only if T2 touches `versioned_paths()`) · declaring the API stable (operator HOLD).
