# MEMENTO

> *"Remember Sammy Jankis."* — long-term memory/relationship engine for 42labs agents.

An LLM has anterograde amnesia: total memory loss between sessions. MEMENTO is the annotated Polaroids and tattoos — a shared engine any 42labs product attaches to remember, and build a relationship with, *its* operator/user.

**One engine, N independent memories.** Shared code, never shared data: each consumer project runs its own instance against its own isolated store (a git-ignored `memory/` directory inside that project's app repo). No central service, no compartments.

## Design

The founding ADR — [`adr-260731-memento-founding.md`](./adr-260731-memento-founding.md) — is the authority (SIGNED 2026-07-31; hardened through six adversarial review rounds). Headlines:

- **Store:** plain human-readable files — append-only JSONL event logs + projected markdown documents; status folded at read time, never stored; `document_replaced` events give documents history + rollback; git only on explicit backup opt-in.
- **Write path:** LLM-distilled consolidation accepted **all-or-nothing** through deterministic gates (schema, derived identity, monotonicity/anti-erosion floor — adapters may tighten, never disable). Runs deferred in a spawn-gated detached subprocess — never on the interactive path.
- **Read path:** token-budgeted always-loaded core prefix + selective recall. The archive is never bulk-loaded.
- **Forgetting:** tombstone, never delete; reconsolidation on retrieval; operator `view`/`edit`/`forget` as first-class verbs.
- **Eval:** deterministic continuity harness (no LLM in the judging loop) gates CI; live tier informs only. No real operator data in any corpus, ever.

## Install

Python ≥ 3.11, `uv`-managed, no runtime dependencies. Consumed as a git-pinned dependency — no PyPI,
no semver ceremony until a second consumer proves the boundary (Phase C).

```toml
# in the consumer's pyproject.toml
dependencies = ["memento @ git+https://github.com/4242labs/memento.git@<sha>"]
```

The pin covers the prompt templates too: they are SHA-verified at load, so they cannot drift under a
consumer that pinned the engine.

```bash
uv sync --extra dev
uv run pytest              # the deterministic tier; no model, no credentials, no token spend
```

## API

Full adapter reference: [`docs/adapter-contract.md`](./docs/adapter-contract.md).

### Store

```python
from memento import MemoryStore, DocumentWrite

store = MemoryStore("./memory")            # store_root IS the namespace; there is no tenancy seam

store.append("errors/fr", [{"id": "fr-x", "pattern": "je suis 20 ans"}],
             session="260731-1354", batch="consolidation")   # idempotent on (session, batch)
store.folded("errors/fr")                  # status folded from history, never stored
store.replace_documents([DocumentWrite("profile.md", text)],
                        session=..., batch=...)              # atomic, and audited
store.document_history("profile.md")       # every document_replaced event, with prior content
```

### Reading

```python
from memento import assemble_prefix, recall

prefix = assemble_prefix(store, adapter)   # budgeted; deterministic truncation; never silent
prefix.text, prefix.tokens, prefix.truncated
recall(store, "kites", limit=8)            # selective; the archive is never bulk-loaded
```

### Writing

```python
from memento import Proposal, apply_consolidation

apply_consolidation(store, adapter, Proposal(facts=..., entries=..., tombstones=...),
                    session=..., batch=..., queue=queue, sink=sink,
                    expected_fingerprint=facts_fingerprint(state.facts))   # required
```

All-or-nothing through the gates: secrets → schema → derived identity → anti-erosion floor →
ordered scales → the adapter's own rules. On failure nothing is written, the session is marked
`deferred`, and a FLAG is raised. `GateFailure.violations` carries every rule that fired.

### Deferred consolidation

```python
queue.close_and_enqueue(session)           # session exit: this and nothing else
spawn_drain(store_root=..., queue_root=..., adapter_ref="app.memory:ADAPTER",
            distiller_ref="app.memory:DISTILLER",
            gate=DrainGate(prefix_materialized=True, idle_seconds=idle))
```

Detached subprocess, spawn-gated by the parent, `flock` claim per session, store lock never held
across the model call, `consolidated` marker written last.

### Operator verbs

```bash
memento --store ./memory status
memento --store ./memory view profile.md
memento --store ./memory history profile.md
memento --store ./memory rollback profile.md
memento --store ./memory edit profile.md
memento --store ./memory forget languages/de --adapter app.memory:ADAPTER
memento --store ./memory recall kites --since 2026-07-01T00:00:00Z --budget 400
memento --store ./memory backup --remote git@github.com:you/private-store.git --yes
```

`forget` writes a tombstone; nothing in this engine deletes an event.

### Agent consumers

The second consumer class is an agent — markdown and a shell, no Python. For it the CLI *is* the
API, and the whole session lifecycle is there: `journal` → `enqueue` → `pending --gate-check` →
`claim` → `prefix` → `consolidate` → `commit` → `done` → `release`. Same gates, same
compare-and-swap, same floor; the agent is simply the distiller as well as the writer.

```bash
memento --store ./memento pending --queue ./q --gate-check --idle-seconds 30 --prefix-materialized
TOKEN=$(memento --store ./memento claim 260802-1400)
memento --store ./memento facts --from-store --adapter-file ./adapter.json   # adoption: bytes win
```

Full contract, exit codes included: [`docs/agent-consumers.md`](./docs/agent-consumers.md).

### Store layout

```
<store_root>/
  profile.md  interests.md      projected documents
  errors/fr.jsonl               event streams
  sessions/log-*.md             free-prose session logs
  .memento/                     engine area
    schema_version  facts.json  documents.jsonl  tombstones.jsonl
    objects/  locks/
```

Byte-compatible with today's `jubs-memory`: adoption moves a path, not data.

## Status

Phase A (clean-room engine build) — [`blocks/a-01-engine.md`](./blocks/a-01-engine.md) — implemented,
deterministic tier green. Phase B (first consumer: jubs) —
[`blocks/b-01-jubs-adoption.md`](./blocks/b-01-jubs-adoption.md) — not started. Phase C (second
consumer proves the adapter boundary) — unscoped by design.

The API is provisional until Phase C. Pin by SHA.

## License

Open source — [AGPL-3.0](LICENSE). Commercial — contact ahoy@42labs.io.

---
If it earned its keep, [coffee is appreciated](https://buymeacoffee.com/42piratas). ☕
