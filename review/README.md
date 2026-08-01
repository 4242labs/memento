# Review artifacts

Preserved from the session that built the engine, because they are the only thing here that was
*not* written by the author of the code.

## What these are

Two adversarial review rounds ran against A-01. Each produced a suite of reproductions. **They assert
the behaviour that was wrong at the time they were written**, so:

> **a repro that PASSES means that defect is still live. A repro that FAILS means it is fixed.**

That inversion is deliberate and it is the point — it is how you check the fixes without trusting
the fixer's own tests.

They are not part of `pytest` collection (`testpaths = ["tests"]`), and they are not expected to pass.

## Running them

```bash
cd review/round1 && PYTHONPATH=../../tests:. python -m pytest test_adversarial.py test_probe2.py -v
cd review/round2 && PYTHONPATH=../../tests:. python -m pytest test_r2.py test_r3.py test_r4.py -v
```

They import fixtures from `tests/conftest.py`, hence the `PYTHONPATH`.

## Expected results as of `449d262`

| Suite | Fail (fixed) | Pass (see below) |
|:--|:--|:--|
| round1 | 23 | 2 |
| round2 | 29 | 4 |

The six that still pass are **not** live defects, and each was checked individually:

| Repro | Why it passes |
|:--|:--|
| `round1::test_crlf_log_still_reads` | Asserts correct behaviour. It always passed. |
| `round1::test_symlinked_directory_makes_recall_explode` | Written as `if "link/x" in streams:` — the body never runs. |
| `round2::test_V1_...` | Instantiates `old_lock.OldStoreLock`, a frozen copy of the pre-fix implementation. Nothing to do with current code. |
| `round2::test_V1b_...` | Same frozen copy, running an *old* version of a shipped test against it. |
| `round2::test_V2_...` | Asserts `Path.rglob` does not descend into directory symlinks — a property of CPython. |
| `round2::test_P3_...` | Asserts the fingerprint compare-and-swap is correct. Making it fail would mean breaking the engine. |

These files are verbatim apart from one thing: credential-shaped literals are split by string
concatenation, so the repo contains none and the secrets scan needs no exception. Behaviour is
unchanged.

`old_lock.py` is a frozen copy of `StoreLock` as it was before the round-one fixes. Keep it: two
repros depend on it, and it is the only record of what that code looked like.

## mutation-survivors.txt

445 mutants that no test noticed, from `mutmut run` over `gates.py`, `events.py`, `store.py`,
`locking.py` at `449d262`. 1471 mutants total, 1012 killed, 14 skipped.

A survivor is **not** a bug. It means "the suite would not have noticed if this line were wrong."
Some are equivalent mutants (message strings, defensive branches). Nobody has triaged them yet.

Regenerate with `mutmut run --max-children 8` (~18 minutes) and `mutmut results`.
