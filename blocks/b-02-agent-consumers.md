# Block B-02: MEMENTO for agent consumers — full write path, declared rules, recall

**Status:** 📋 To do
**Repo:** `4242labs/memento` (+ ADR amendment; jubs follows via pin bump + 42L-1236-style owned step for the rename)
**Depends on:** A-01, B-01 (shipped). PR **memento#8** (declarative adapters + CLI `prefix`/`consolidate`/`facts`) is the foundation — review and land it first, then build on it.
**Created:** 2026-08-02
**Authority:** founding ADR (SIGNED) + operator orders 2026-08-02: MEMENTO must fully work with regular agents (e.g. `tortuga/agents/advisor-legal` — markdown + a shell, no Python); close the paper gaps (agent-initiative recall). Phase-C "API stable" declaration stays **HELD** (operator 2026-08-02) — everything here ships under provisional API + SHA pins.

**Required reading:** `docs/handoff-a-01.md` §5 · `docs/adapter-contract.md` · PR #8 body (the shell-boundary CAS contract: `--expect`/`--unchecked`, exit codes 3/4/5).

---

## Tasks

### T1: Land PR #8
Adversarial review, then merge. It is the base every task below extends. The `uv.lock` tracking change stays in (lockfile belongs on main).

### T2: CLI queue + session-exit path *(gap 1)*
Agents can't journal or enqueue — there is no session-exit contract outside Python. Add verbs over the existing `queue.py`/`drain.py`: `journal` (append turn material), `enqueue` (close + mark pending), `claim`/`release` (the D2 ephemeral claim, PID+TTL), `pending` (list + staleness, the D3.5 backlog FLAG surface). Same crash-safety invariants: marker-LAST, idempotent re-run, claim ≠ marker.

### T3: Agent-as-distiller consolidation *(gap 3)*
The drain assumes the engine calls a Python `Distiller`. An agent **is** the model — the flow inverts: agent reads `prefix` + pending journal, produces a proposal JSON itself, submits via `consolidate --proposal <file> --expect <fp>`. Ship the loop as a documented contract (`docs/agent-consumers.md`): read → distill → submit → on exit 3/4/5 do X. The gates stay engine-side and non-negotiable — the agent is the writer the gates were built to distrust (MemSyco-Bench posture unchanged).

### T4: Declared domain rules *(gap 2)*
Spec adapters get only the floor; apps can tighten, agents can't. Add a declarative tighten-only rule vocabulary to `memento.spec` — ordered scales (≤1 step), set-shrink prohibitions, regex/enum field constraints, required-member lists. No custom code, no loosening: a spec rule that would weaken the floor is a spec error, refused at load (unknown keys stay refused, never ignored).

### T5: Existing-store adoption for declared adapters *(gap 4)*
`facts_from_store` is code-only, so a declared consumer can't adopt a store that already has documents. Expose it through the spec/CLI path (`facts --from-store`), same B-01 rule: **bytes win over re-projection** — divergence FLAGs and defers, never rewrites.

### T6: Recall verb *(paper gap — agent-initiative retrieval)*
`recall <query>` — structured search over events + documents (stream, key, date-range, text match), token-budgeted output, engine-side. This is D4(b) made real for agents mid-session: query when relevant instead of prefix-only. No vectors (ADR §3 rejection stands at this scale).

### T7: Store dir rename `memory/` → `memento/` *(gap 5 — canon-touching)*
Default store dir becomes `memento/` (kills the `/memory/`-vs-`src/*/memory/` gitignore trap class outright). ADR amendment + jubs follows: `jubs-app/memory/` → `jubs-app/memento/` as a B-01-style owned move (path config + `.gitignore` + backup remote untouched). **Operator sign-off required before the jubs half executes.**

### T8: Mutation coverage on the new code *(gap 6)*
Every module this block adds or extends (`cli`, `spec`, queue verbs, recall) gets a mutmut run; survivors triaged or covered before merge. The pre-existing 445 survivors stay 42L-1239 — this block only refuses to add to the pile.

## Acceptance Criteria

| # | Criterion | Verification |
|:--|:--|:--|
| AC-1 | A non-Python agent completes the full loop through CLI alone: journal → enqueue → claim → prefix → proposal → gated consolidate → backup push | `test (subprocess-only harness)` |
| AC-2 | Spec-declared domain rules enforce (tighten) and cannot loosen the floor; loosening spec refused at load | `test` |
| AC-3 | Declared adapter adopts the jubs-layout fixture store; bytes-win on divergence | `test` |
| AC-4 | `recall` returns budgeted, deterministic results; zero cross-topic contamination probe passes | `test` |
| AC-5 | Rename landed engine-side + ADR amended; jubs half gated on operator sign-off | `review + cmd:git log` |
| AC-6 | Suite green; mutmut on new/changed modules — no new survivors | `ci + cmd:mutmut` |
| AC-7 | `docs/agent-consumers.md` documents the full agent contract (loop, exit codes, CAS, claim TTL) | `review` |

## Out of Scope
Hierarchical/temporal abstraction (Phase C, data-hungry — ADR D7) · vectors/embeddings · MCP face (needs operator data-boundary ruling, ADR §2) · the 445 pre-existing survivors (42L-1239) · shim retirement (42L-1255) · queue-material coupling fix (42L-1257 — engine-side, may ride along only if T2 touches `versioned_paths()`) · declaring the API stable (operator HOLD).
