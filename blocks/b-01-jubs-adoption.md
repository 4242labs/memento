# Block B-01: jubs adopts MEMENTO (ADR D9 Phase B)

**Status:** 📋 To do
**Repos:** `42piratas/jubs-app` (+ jubs-meta canon via grant flow)
**Depends on:** A-01, jubs block 06-01 (Phase-6 build — its deferral contract already matches the engine's D3)
**Blocks:** — (live validation folds into the jubs 06-02 operator sitting)
**Merge approval:** jubs two-gate flow (PRs → `staging`)
**Created:** 2026-07-31
**Authority:** `adr-260731-memento-founding.md` (SIGNED) + jubs founding ADR rev 6. **Required reading before T1:** `docs/handoff-a-01.md` §5 (traps — esp. `identity_keys` add-never-displace, empty rule set fails closed) + `docs/adapter-contract.md`.

---

## Tasks

### T1: Adapter swap
Add MEMENTO as a dependency **pinned by commit SHA** (API is provisional until Phase C); write the jubs adapter (taxonomy: errors/{lang}, interests, profile, **vocab** — the store's full layout incl. `vocab_id`/`vocab_review_list` is the compatibility target, nothing in it is out of taxonomy; distillation prompt; recall policy + token budget; retention: **keep everything** per operator 2026-07-31). Replace `src/jubs/memory/` internals with engine calls behind the existing interfaces — the full jubs test suite stays green unmodified (behavioral parity is the gate). **The shim modules this implies are transitional, not the end state**: they exist so parity is provable during the swap; retiring jubs' duplicated memory tests + shims in favour of the engine's own suite is follow-up 42L-1255, never this block. With the real distiller now available, run memento's live tier (`tests/live/`, non-gating, tolerance-based) — its first-ever execution; FLAG divergences.

**Engine-defect protocol (applies to the live tier, the facade, anything):** never patch the engine in-tree in jubs. A contained defect with test coverage → PR to `4242labs/memento` under A-01's merge rule (auto on CI-green), bump the SHA pin, continue. Anything touching ADR-level behavior (gates, floor, claim/lock semantics) → FLAG + card, stop that thread.

### T2: Store move (owned step, pre-approved)
Relocate `~/42labs/jubs/jubs-memory` → `jubs-app/memory/`, contents byte-identical; root-anchored `/memory/` in `.gitignore` (NEVER `memory/` — would untrack `src/jubs/memory/`); `memory.path` config updated. **Bytes win over re-projection:** if `render_documents(facts_from_store(store))` does not reproduce the existing `profile.md`/`interests.md` bytes, keep the store's bytes untouched, FLAG the divergence, and defer — a one-time re-projection of operator memory is the operator's call, never the block's. Enable backup: fresh **private** remote, explicit opt-in recorded (operator 2026-07-31). Then **delete** the old local repo AND `42piratas/jubs-memory` on GitHub (operator decision 2026-07-31 — pre-move history knowingly discarded).

### T3: Canon fold
jubs-meta via grant flow: rev-6 §3.5/§3.12 amendment (store path, engine dependency, backup posture) + pipeline row. §3.2 abort behavior is decided by the operator at the 06-02 sitting (already in its Inbox) — do not close it here.

## Acceptance Criteria

| # | Criterion | Verification |
|:--|:--|:--|
| AC-1 | Full jubs suite green on the engine-backed memory path, tests unmodified — both suites at the v1.2.1 baseline (409 pytest + 19 Playwright) | `test` |
| AC-2 | Store moved byte-identical; live session reads prior memory correctly (session N+1 references N) | `cmd:diff + smoke session` |
| AC-3 | Private backup pushes; old local repo + GitHub repo deleted | `cmd:gh repo view (404)` |
| AC-4 | Canon amendment landed via grant flow | `cmd:git log jubs-meta` |
| AC-5 | memento live tier executed against the real distiller (first run); results reported, divergences FLAGged | `cmd:pytest tests/live` |

## Out of Scope
§3.2 decision (06-02 sitting) · engine changes **in-tree in jubs** (engine fixes go to memento per the defect protocol above) · shim/test retirement (42L-1255) · Phase C · mutation-survivor sweep (backlogged: 42L-1239).
