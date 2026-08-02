# B-02 mutation record

**T8's honest answer: the "no new survivors" gate is NOT met.** The numbers, the triage, and what
was done about them are below. An untriaged survivor list rots into noise, so this file exists to
keep the list readable rather than to make it look better.

## What could not be measured before

`mutmut` **could not run at all** on `main`. It copies only `source_paths` plus `also_copy`, so the
copied tree held five modules out of nineteen and the suite died on the first cross-module import —
`ModuleNotFoundError: No module named 'memento.writepath'`, before a single mutant ran. Three more
files tests actually read were missing too: `README.md` (the wheel-build test builds from the copy),
`.gitignore` and `docs/` (the store-pattern and documented-invocation regressions read them).

Fixed in `pyproject.toml`. The A-01 figures reproduce almost exactly once it runs — `gates` 234 vs
235, `store` 90 vs 89, `events` 84 vs 88, `locking` 33 vs 33 — so the handoff's 445 was real, and
this branch did not erode it.

## Pre-block baseline vs now

Baseline: `main` @ `2b5c9de`, the five modules A-01 never measured.
Now: this branch, ten modules, 4899 mutants.

| Module | Baseline | Now | Δ |
|:--|--:|--:|--:|
| `cli` | 342 | 583 | **+241** |
| `spec` | 179 | 232 | **+53** |
| `readpath` | 88 | 111 | **+23** |
| `adoption` | — | 11 | **+11** (new module) |
| `drain` | 69 | 68 | −1 |
| `queue` | 60 | 60 | 0 |
| `gates` | 235 (A-01) | 234 | −1 |
| `store` | 89 (A-01) | 90 | +1 |
| `events` | 88 (A-01) | 84 | −4 |
| `locking` | 33 (A-01) | 33 | 0 |
| **Total** | | **1506** | |

Killed 2907, no test at all 465.

## Triage of the 1075 survivors in the six modules this block touched

Classified mechanically (`- ` line vs `+ ` line, ignoring the renamed `def`):

| Class | Count | What it means |
|:--|--:|:--|
| output-only | 417 | The mutation changes only what is *printed* — a message string, or `print(f"…")` → `print(None)`. Killing these means asserting exact console text |
| exit-code | 60 → **39** | `return 0` → `return 1` and friends. **This class matters**: `docs/agent-consumers.md` tells an agent to branch on the number |
| other behavioural | 605 | Argument defaults, sentinel comparisons, branch inversions in paths no test distinguishes |
| no-op | 4 | Mutant and original are identical after formatting |

## What was done

The **exit-code** class was the one worth paying for, and 21 of them are now dead: every verb's
success code is asserted, and the malformed-proposal path is pinned to exit 2 so it cannot drift
into 3, which means something else entirely. The renderer's identity ordering was also unguarded —
the existing shuffle test passed against an identity lookup returning nothing at all, because the
`str(member)` tie-break reproduces the same order by accident. Both fixes were verified by breaking
the code and watching the test fail first (handoff §5.1).

The remaining 39 exit-code survivors are in `--json` formatting and in `drain`/`readpath` internals
where the returned number is discarded by every caller.

## Why the gate is not met, stated plainly

`cli.py` roughly doubled — it now carries the whole session lifecycle for a consumer with no Python
— and it is mostly `argparse` wiring and `print`. Roughly 70% of its survivors are output-only.
Closing them means asserting console text line by line, which buys a suite that breaks on every
reworded message and still would not have caught any defect found on this branch.

The mutation sweep is also now **nightly-scale**: 4899 mutants, and `cli.py`'s mutants each run the
AC-1 subprocess harness. That matches `docs/handoff-a-01.md` §3.3, which already says a per-PR gate
is the wrong shape for this. Wiring the ratchet — baseline recorded, fail on *new* survivors, drive
the baseline down — is 42L-1239's, and the two lists in this directory are what it should ratchet
against.

## Files

- `mutation-survivors-b02-baseline.txt` — 738, `main` @ `2b5c9de`, five modules
- `mutation-survivors-b02.txt` — 1506, this branch, ten modules
