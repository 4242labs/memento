# Contributing

**Status: passively maintained.** MEMENTO is used in production at 42labs and gets commits
regularly — but it is not a staffed product. There is no support rota and no SLA. Issues
and pull requests are welcome and genuinely read; expect a reply in weeks rather than
days, and sometimes not at all. That is capacity, not disinterest. Plan accordingly
before you invest a weekend.

## What's welcome

- **Bug reports with a reproduction.** The smaller the repro, the faster it moves.
- **Small, focused pull requests.** One logical change, tests green.
- **Documentation** — typos, unclear passages, missing setup steps. Always welcome, usually fast.

## What is unlikely to land

- Large refactors, architecture changes, rewrites.
- Features not discussed in an issue first. **Open the issue before you write the code** — one message, potentially a saved weekend.
- Unrequested dependency bumps, formatting-only diffs, build-tooling swaps.

## If you need it faster

Fork it. The AGPL-3.0 grants you exactly that. A fork that moves faster than this repo is
a good outcome, not a betrayal — this is a real answer, not a brush-off.

## Before you open a PR

```bash
uv sync --extra dev
uv run pytest                               # the deterministic tier — no model, no credentials
uv run python -m memento.templates --check  # template pins match what is on disk
```

CI runs that across Python 3.11 / 3.12 / 3.13, plus a gitleaks scan of the full history.

Two things this repo is strict about, because both fail silently:

- **The store is data, never code.** A committed store lands in public. CI refuses one.
- **Fixtures are synthetic.** Build credential-shaped test data with
  `tests/support/fake_credentials.py` — never by hand, or the secret scanner trips on your PR.

If you touched a write gate, the event log, the store or the lock protocol, the mutation
suite is what turns "the tests are green" into evidence rather than a claim:

```bash
MEMENTO_MUTATION=1 uv run mutmut run
```

## Licensing

MEMENTO is dual-licensed: AGPL-3.0 for open source, commercial terms on request — see
[LICENSING.md](LICENSING.md).

**By submitting a pull request you grant 42labs the right to distribute your contribution
under both the AGPL-3.0 and 42labs' commercial license.** You keep the copyright to what
you wrote. Without this grant a single merged patch would make the commercial half
unsellable, and we would have to refuse it.
