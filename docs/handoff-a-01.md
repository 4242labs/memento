# A-01 handoff — MEMENTO engine

**Written:** 2026-07-31, at the end of the session that built it.
**State of record:** `main` = `52f857e` (merged). Open work: **PR #2** (`chore/alberto-20260731-a-01-round2`, head `449d262`).
**Authority:** [`adr-260731-memento-founding.md`](../adr-260731-memento-founding.md) — SIGNED. Where anything here disagrees with the ADR, the ADR wins.
**Block:** [`blocks/a-01-engine.md`](../blocks/a-01-engine.md) — status field untouched (process layer, not this agent's to edit).
**Linear:** 42L-1235.

This is a full account, including the parts that went badly. Read §5 before writing any code.

---

## 1. What exists

A Python library, `uv`-managed, zero runtime dependencies, in `src/memento/` (19 modules). It
implements ADR decisions D1–D8. jubs' `src/jubs/memory/` was never read or copied — clean-room per
D9 — and its *store layout* is the compatibility target, covered by a fixture rather than a
migration.

| Module | What it owns |
|:--|:--|
| `store.py` | `MemoryStore`: paths, streams, projected documents, `document_replaced` history, object area, namespace containment |
| `events.py` | Append-only JSONL, idempotent batch append, tail repair, permissive reads |
| `fold.py` | Status folded at read time; supersession, retirement, contradiction |
| `gates.py` | Schema, derived identity, anti-erosion floor, ordered scales — **the write gates** |
| `writepath.py` | `apply_consolidation`: all-or-nothing, fingerprint compare-and-swap |
| `drain.py` | Spawn-gated detached subprocess, per-session claim, per-session failure isolation |
| `queue.py` | Journals, pending log, `consolidated` marker (LAST), backlog bounds, retention |
| `locking.py` | Store lock (per-path state, per-thread reentrancy), session claims |
| `readpath.py` | Budgeted core prefix, deterministic truncation, selective recall |
| `forgetting.py` | Tombstones, reconsolidation, document rollback |
| `backup.py` | Opt-in git, attribution, explicit staging |
| `secrets.py` `ids.py` `tokenizer.py` `adapter.py` `templates.py` `flags.py` `clock.py` `errors.py` `cli.py` | Support |

232 tests in `tests/`. CI on 3.11–3.13 plus gitleaks and a tracked-store check. Docs:
[`docs/adapter-contract.md`](adapter-contract.md), API section in the README.

---

## 2. What was done, in order

1. **Built the engine** against the ADR. 172 tests. Called it green, opened PR #1.
2. **Round-one adversarial review** (dispatched on operator instruction, not proactively): **17 defects, 3 critical**. All demonstrated with repros. Fixed; +44 tests.
3. **Merged** PR #1 to `main` on operator instruction, before the second review returned. Cleaned branches, synced locals, revalidated on trunk — which passed, because the trunk validation used the same suite that had missed everything.
4. **Round-two adversarial review**, against the *fixes*: **10 more defects, 3 critical**, including one the fixes introduced that was worse than the bug it replaced. Fixed on the current branch; +14 tests. This is PR #2.
5. **Mutation testing** (`mutmut`, scoped to the four correctness-critical modules): **445 of 1471 mutants survive**. See §4.

Full defect lists are in the commit messages of `52f857e` and on PR #2. The repros are preserved in
[`review/`](../review/README.md).

---

## 3. What must be done, in priority order

### 3.1 — Land or discard PR #2

It closes all ten round-two defects; CI is green. Nothing consumes the engine yet, so there is no
urgency and no risk either way.

### 3.2 — Kill the mutation survivors, `gates.py` first

`gates.py` holds the anti-erosion floor — the ADR's non-disableable defence against a model quietly
deleting things. **235 of its mutants survive.** A silent pass there is data loss, so this is the
highest-value work in the repo.

Order: `gates.py` (235) → `store.py` (89) → `events.py` (88) → `locking.py` (33).

Method that works: take a survivor from `review/mutation-survivors.txt`, apply that mutation by
hand, write a test that fails, revert the mutation, watch it pass. **Never the other way round** —
see §5.1.

Expect a real fraction to be equivalent mutants (message strings, defensive branches). Triage
honestly and record which were dismissed and why; an untriaged survivor list rots into noise.

### 3.3 — Wire mutation testing into CI as a ratchet

Config is already in `pyproject.toml` under `[tool.mutmut]`. The full core sweep takes ~18 minutes,
which is too slow per-PR but fine nightly. Standard shape: record today's survivor set as a
baseline, fail the build on any *new* survivor, and drive the baseline down. Do not gate on the
absolute number or nothing will ever merge.

Note the other 14 modules are **not measured at all** yet.

### 3.4 — Extend the measured surface

`writepath.py`, `drain.py`, `queue.py`, `readpath.py`, `forgetting.py` all carry correctness
weight and are outside the current `source_paths`.

### 3.5 — Before B-01

- The **live tier** (`tests/live/`) has never run. It needs a real distiller, which arrives with the
  first consumer. Until it runs, "the prompt and the gates agree" is unverified.
- `.worktrees/` is not in `.gitignore` and shows as untracked in a `main` checkout. One line.
- The API is provisional until a second consumer exercises the adapter boundary (Phase C). Pin by SHA.

---

## 4. Measured state as of `449d262`

| Measure | Value |
|:--|:--|
| Tests | 232 pass, 2 live deselected |
| CI | green, 3.11 / 3.12 / 3.13, gitleaks, tracked-store check |
| Mutants killed | 1012 / 1471 (69%) |
| **Mutants survived** | **445** — `gates` 235, `store` 89, `events` 88, `locking` 33 |
| Modules measured | 4 of 19 |
| Round-one repros still passing | 2, both benign (see `review/README.md`) |
| Round-two repros still passing | 4, all benign |
| Defects found and fixed | 27 (17 + 10) |
| Defects found by the author's own tests | **0** |

That last row is the one that matters.

---

## 5. Traps — read before writing code

### 5.1 A test written after the fix proves nothing

Three shipped regression tests passed against the very code they were written to guard:

- two thread-safety tests raced four threads at once, and every thread cleared the depth check
  before the first one set it — the broken lock passed;
- a symlink test used a *directory* symlink, which `Path.rglob` never descends into on any supported
  Python, so the assertion was true before the fix existed.

Both were caught only because a reviewer went looking. **Write the test, watch it fail against the
broken code, then fix.** Mutation testing is the mechanical version of this rule.

### 5.2 The redrive is the normal case, not an error

A drain keys its batch on the session id. A crash between the `document_replaced` event and the file
swap means the next drain re-runs the model, and **a re-run model does not repeat itself byte for
byte**. Any code path that treats "same key, different content" as a conflict wedges that session
permanently. This was shipped twice — fixed for documents, then reintroduced for entry streams one
file over. Both now land as new revisions.

### 5.3 Event-first, file-second, and why

`replace_documents` appends the event carrying the prior content *before* swapping the file. Reverse
it and a crash destroys the prior content irrecoverably on a git-less store. The replay path is what
makes event-first safe; do not "simplify" it.

### 5.4 The floor must fail closed

Anything the anti-erosion floor cannot verify — a list member with no recognised identity, two
members with the same identity, a collection that changed kind — is a **violation**, not a skip. A
check that silently declines to run reads as a green light. The first build shipped exactly that, and
an adapter could disable the floor by naming its identity field `lang`.

Related: adapter `identity_keys` may **add** to the engine's keys, never displace them. A superset
searched first put a non-identity field ahead of a real one and blinded the floor.

### 5.5 Facts paths are key tuples, never dotted strings

`node.js`, `pt.br`, `arXiv:2604.06710` are ordinary keys. Dotted path strings mis-split on them,
which made an identical no-op proposal read as a deletion and — writes being all-or-nothing — locked
the store permanently. Markers escape the separator (`path_marker` / `split_marker`); keep them
symmetric.

### 5.6 Gates cannot check prose

The projected documents are LLM-authored markdown and are not reconstructible from the log. The
structured `facts` in `.memento/facts.json` are what the gates actually read. Anything that only
touches the markdown is unguarded.

### 5.7 Locks

`flock` is per open file description, so two handles in one process *do* exclude each other — which
is why an operator `forget` inside a held lock used to deadlock for the full timeout. State is now
per store path with per-thread reentrancy, and rebuilt on a new pid so a forked child does not
inherit a depth of 1 and skip the flock entirely.

Never hold the store lock across the model call or across `pull`/`push`.

### 5.8 Everything a session id touches must validate it

The queue and lock directory build paths straight from session ids. Unvalidated, `../../x` escapes
the queue and — with pruning enabled — deletes outside it.

### 5.9 The secrets gate sits at the last door

It is in `store.append` / `replace_documents` / `write_session_log`, not only in
`apply_consolidation`, because a gate that guards one path is not a gate. Two consequences worth
remembering: a credential-bearing remote URL is refused *before* `git init` (it would otherwise be
persisted in plain text), and **rollback is exempt** — restoring content already in the log is
recovery, and a gate that blocks it takes away the operator's only lever.

### 5.10 `expected_fingerprint` is required, deliberately

No default. Defaulting it to "check nothing" lost writes between concurrent drains; defaulting it to
"check what we gated against" narrowed the window without closing it and hid the decision. A caller
with no baseline passes `writepath.UNCHECKED` and says so.

---

## 6. Decisions worth knowing

| Decision | Why |
|:--|:--|
| Structured facts in `.memento/facts.json` | Gates need data, not markdown. Written as a projected document so it carries `document_replaced` history and rolls back like anything else. |
| Document batch ids carry a content digest | Distinguishes a replay (same bytes) from a redrive (different bytes) without either duplicating events or refusing the write. |
| Abandoned revisions retired by their own event | Supersession is an event everywhere else in this store. An inline note on the successor left the chain looking broken to any reader that did not know to check for it. |
| Facts depth capped at 64 | An unbounded walk raised `RecursionError`, which is not a `MementoError`, so no caller could catch it and the drain turned it into a permanent deferral. |
| Backup stages explicit paths, not `add -A` | The store root is not the engine's alone. A consumer queue under it — jubs' `sessions-data` is the obvious case — was being committed and pushed. |
| Live tier non-gating | A flaky model day must not turn the build red. The deterministic tier gates precisely because it needs no model. |
| No gitleaks path allowlist | Path exceptions cover everything added to that file afterwards. Per-finding fingerprints in `.gitleaksignore` die when the line moves, so they cannot widen. |
| One fixture factory for fake credentials | `tests/support/fake_credentials.py` assembles them at runtime. A literal gets committed, and a secret in history costs a rewrite or an exception — neither is free. |
| Pre-commit hook | `git config core.hooksPath .githooks`. Prevention is the only cheap fix; CI greps as the backstop for anyone who has not enabled it. |
| Squash-merge anything carrying a gitleaks fingerprint | Fingerprints are commit-pinned. A rebase merge rewrites the commit, every pin misses, and `main` goes red — which is exactly what happened landing PR #2. |

---

## 7. Open questions for the operator

1. **Fix or rebuild.** Nothing consumes the engine, no store exists, `main` is one commit. A rebuild
   costs only the time already spent — and would repeat this outcome unless review is in the loop
   from the first commit rather than at the end.
2. **How much mutation coverage is enough**, and does it gate merges or only report.
3. Whether B-01 waits for a clean mutation sweep or starts against the current engine.

---

## 8. If you are the agent picking this up

Start here:

1. Read the ADR in full. Every decision D1–D9 is load-bearing and several read as pedantic until you
   hit the failure they prevent.
2. Read [`review/README.md`](../review/README.md) and run both repro suites. They are the only
   assessment in this repo not written by the code's author.
3. Read `tests/test_regressions.py`. It is the list of things the first two builds believed and got
   wrong, one docstring at a time.
4. Then §3, in order.

Do not trust a green suite here. It has been green and wrong twice.
