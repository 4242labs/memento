# B-02 mutation record

Post-split state, under the architect's T8 amendment (memento `765a360`). The earlier version of
this file reported the original gate as **not met**; that finding is what the amendment answers, and
what follows is measured against the amended gate.

## What could not be measured before

`mutmut` **could not run at all** on `main`. It copies only `source_paths` plus `also_copy`, so the
copied tree held five modules out of nineteen and the suite died on the first cross-module import —
`ModuleNotFoundError: No module named 'memento.writepath'`, before a single mutant ran. Three more
files tests actually read were missing too: `README.md` (the wheel-build test builds from the copy),
`.gitignore` and `docs/` (the store-pattern and documented-invocation regressions read them).

Fixed in `pyproject.toml`. The A-01 figures reproduce once it runs — `gates` 232 vs 235, `store` 90
vs 89, `events` 84 vs 88 — so the handoff's 445 was real, and this branch did not erode it.

## The ratchet boundary is architectural

`presentation.py` and `cli.py` carry no `source_paths` entry, and that is a **boundary, not an
exclusion list**:

* Everything a consumer may depend on — the exit code and the `--json` payload — is decided in
  `commands.py`, which is in scope.
* `presentation.py` holds human-facing strings and nothing else. Its renderers never decide a code,
  never touch the store, and never print. A mutant there cannot change a promise.
* `cli.py` is argparse and dispatch.

If a mutant in a non-scope module could ever alter a promise, the split is wrong — not the ratchet.
That is a cheaper invariant to hold than a list somebody has to keep in sync.

## R3: the kill mechanism is in-process, and it is what made this affordable

`tests/test_agent_loop.py` is marked `acceptance`; `MEMENTO_MUTATION=1 uv run mutmut run` drops it
from the per-mutant runner. Before that, every `cli.py` mutant paid for dozens of interpreter
starts: the sweep ran at **2.4 mutations/second** and stalled for hours in that region. The
in-process contract table covers the same ground and a mutant dies in milliseconds.

The acceptance tier still runs in CI and in a plain `pytest` — it is what proves the loop composes
across real process boundaries, which no in-process test can.

## Post-split baseline — the number the ratchet works from

4435 mutants, 2926 killed, 117 with no test, **1333 survivors**.

| Module | In ratchet scope | Survivors |
|:--|:--|--:|
| `commands` | **yes** | 400 |
| `spec` | **yes** | 232 |
| `readpath` | **yes** | 111 |
| `adoption` | **yes** | 11 |
| `gates` | yes (A-01) | 232 |
| `store` | yes (A-01) | 90 |
| `events` | yes (A-01) | 84 |
| `drain` | yes (A-01) | 69 |
| `queue` | yes (A-01) | 59 |
| `locking` | yes (A-01) | 45 |
| `presentation`, `cli` | **no — by architecture** | not measured |

Recorded in `mutation-survivors-b02.txt`. The pre-block baseline for the five modules A-01 never
measured is in `mutation-survivors-b02-baseline.txt` (738, `main` @ `2b5c9de`). Both are what
42L-1239's nightly down-only ratchet should compare against.

## Every exit-code mutant is dead

The amendment's hard requirement, checked mechanically: a mutant counts if the set of `EXIT_*`
constants or literal return codes in the function changes. Across all 1333 survivors, **one**
remains, and it is provably equivalent:

```
memento.commands._ok
  - return Outcome(code=EXIT_OK, kind=kind, data={"ok": True, **data})
  + return Outcome(kind=kind, data={"ok": True, **data})
```

`Outcome.code` defaults to `EXIT_OK`, so the two forms are the same object. **Marked equivalent.**

Two were real and are now dead. Both were in `cmd_commit`'s push-failure branch, which returned
success when the write had landed but the backup had not — an agent would have recorded a session
as safely copied that was not. `tests/test_exit_codes.py` now drives a store with an unreachable
remote and pins that path to exit 1.

## Equivalent mutants, recorded rather than chased

| Where | Why it is equivalent |
|:--|:--|
| `commands._ok` — omitted `code=EXIT_OK` | The dataclass default is `EXIT_OK` |
| 4 no-ops from the pre-split sweep | Mutant and original are identical after formatting; nothing to kill |

## `locking` — a regression R3 caused, and closed

Excluding the acceptance tier removed the only tests covering `CasClaim`, and `locking` went 33 →
**91**. That was the ruling working as intended (the acceptance tier is not a mutation runner) and
the coverage gap it exposed being real. `tests/test_locking.py` now covers the claim in-process —
record round-trip, torn and partial files, TTL expiry and reclaim, token-checked release, the
listing — bringing it to **45**. The remaining 12 above A-01's 33 are in `CasClaim._write`'s
durability calls (`flush`, `fsync`, the temp-file rename), which no test can observe without a
crash harness.

## Cadence

Two-tier, per the ruling. Per-PR: incremental, changed modules only. Nightly: full sweep, down-only
against the files in this directory, regressions FLAG and open a card. The nightly is **42L-1239's**
to build; this ruling is its spec and these baselines are its input.

## Files

- `mutation-survivors-b02-baseline.txt` — 738, `main` @ `2b5c9de`, the five previously unmeasured modules
- `mutation-survivors-b02.txt` — 1333, post-split, ten modules
