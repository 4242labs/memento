# Block B-01: jubs adopts MEMENTO (ADR D9 Phase B)

**Status:** 📋 To do
**Repos:** `42piratas/jubs-app` (+ jubs-meta canon via grant flow)
**Depends on:** A-01, jubs block 06-01 (Phase-6 build — its deferral contract already matches the engine's D3)
**Blocks:** — (live validation folds into the jubs 06-02 operator sitting)
**Merge approval:** jubs two-gate flow (PRs → `staging`)
**Created:** 2026-07-31
**Authority:** `adr-260731-memento-founding.md` (SIGNED) + jubs founding ADR rev 6.

---

## Tasks

### T1: Adapter swap
Add MEMENTO as a git-pinned dependency; write the jubs adapter (taxonomy: errors/{lang}, interests, profile; distillation prompt; recall policy + token budget; retention: **keep everything** per operator 2026-07-31). Replace `src/jubs/memory/` internals with engine calls behind the existing interfaces — the full jubs test suite stays green unmodified (behavioral parity is the gate).

### T2: Store move (owned step, pre-approved)
Relocate `~/42labs/jubs/jubs-memory` → `jubs-app/memory/`, contents byte-identical; root-anchored `/memory/` in `.gitignore` (NEVER `memory/` — would untrack `src/jubs/memory/`); `memory.path` config updated. Enable backup: fresh **private** remote, explicit opt-in recorded (operator 2026-07-31). Then **delete** the old local repo AND `42piratas/jubs-memory` on GitHub (operator decision 2026-07-31 — pre-move history knowingly discarded).

### T3: Canon fold
jubs-meta via grant flow: rev-6 §3.5/§3.12 amendment (store path, engine dependency, backup posture) + pipeline row. §3.2 abort behavior is decided by the operator at the 06-02 sitting (already in its Inbox) — do not close it here.

## Acceptance Criteria

| # | Criterion | Verification |
|:--|:--|:--|
| AC-1 | Full jubs suite green on the engine-backed memory path, tests unmodified | `test` |
| AC-2 | Store moved byte-identical; live session reads prior memory correctly (session N+1 references N) | `cmd:diff + smoke session` |
| AC-3 | Private backup pushes; old local repo + GitHub repo deleted | `cmd:gh repo view (404)` |
| AC-4 | Canon amendment landed via grant flow | `cmd:git log jubs-meta` |

## Out of Scope
§3.2 decision (06-02 sitting) · any engine changes (fix in A-01/memento) · Phase C.
