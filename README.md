# MEMENTO

> *"Remember Sammy Jankis."*

Long-term memory for LLM agents and apps. An LLM has anterograde amnesia — total memory loss
between sessions. MEMENTO is the annotated Polaroids and tattoos: a small engine your agent or
app attaches to remember, and build a relationship with, its user across sessions.

**One engine, N independent memories.** Shared code, never shared data: each consumer runs its
own instance against its own isolated store — a git-ignored directory of plain, human-readable
files inside the consumer's own repo. No central service, no database, no vector store, no
tenancy seam.

## What makes it different

Most agent-memory systems trust the model to write its own memory. MEMENTO doesn't. The model
*proposes*; deterministic gates *decide*:

- **Validated writes, all-or-nothing.** An LLM-distilled consolidation is accepted only if it
  passes every gate: secrets scan, schema, derived identity, and a monotonicity/anti-erosion
  floor (facts shrink only by explicit tombstone; ordered scales move at most one step).
  Adapters may tighten the rules, never disable them. On failure nothing is written.
- **Append-only history.** JSONL event logs plus projected markdown documents; status is folded
  at read time, never stored. `forget` writes a tombstone — nothing in this engine deletes an
  event. Document replacements carry the prior content, so every document has history and
  rollback.
- **Off the interactive path.** Session exit does one cheap thing: enqueue. Consolidation runs
  later — deferred, claim-protected, compare-and-swap guarded — never while the user waits.
- **Budgeted reads.** A token-budgeted, always-loaded core prefix plus selective recall. The
  archive is never bulk-loaded into context.
- **Human-legible.** The store is markdown and JSONL you can open, `view`, `edit`, and audit.
  Backup to a private git remote is an explicit opt-in, never a default.

## Install

Python ≥ 3.11, zero runtime dependencies. Consumed as a git-pinned dependency — the API is
provisional, so pin by commit SHA (the pin covers the prompt templates too; they are
SHA-verified at load).

```toml
# in the consumer's pyproject.toml
dependencies = ["memento @ git+https://github.com/4242labs/memento.git@<sha>"]
```

Or install the CLI on its own:

```bash
uv tool install "memento @ git+https://github.com/4242labs/memento@<sha>"
```

For development:

```bash
uv sync --extra dev
uv run pytest    # deterministic tier; no model, no credentials, no token spend
```

## Two ways in

### 1. Agents — the CLI is the API

An agent consumer is markdown and a shell: no Python, no imports. It declares its adapter in a
JSON file and drives the whole session lifecycle through the CLI — `journal` → `enqueue` at
session exit, then later: `pending --gate-check` → `claim` → `prefix` → distill → `consolidate`
→ `done` → `release`. Same gates, same compare-and-swap; the agent is simply the distiller as
well as the writer.

```bash
memento --store ./memento prefix --adapter-file ./adapter.json --json
memento --store ./memento journal 260802-1400 --queue ./memento/.queue --text "..." --json
memento --store ./memento enqueue 260802-1400 --queue ./memento/.queue --json
```

`--json` output and the exit codes are the contract; console prose is not. The full lifecycle,
exit codes included: [`docs/agent-consumers.md`](./docs/agent-consumers.md).

### 2. Python applications — the library

```python
from memento import MemoryStore, assemble_prefix, recall, Proposal, apply_consolidation

store = MemoryStore("./memento")           # store_root IS the namespace

prefix = assemble_prefix(store, adapter)   # budgeted; deterministic truncation; never silent
recall(store, "kites", limit=8)            # selective; the archive is never bulk-loaded

apply_consolidation(store, adapter, Proposal(facts=..., entries=..., tombstones=...),
                    session=..., batch=..., queue=queue, sink=sink,
                    expected_fingerprint=...)   # all-or-nothing through the gates
```

Deferred consolidation runs in a spawn-gated detached subprocess — `queue.close_and_enqueue()`
at session exit, `spawn_drain(...)` later. The adapter — taxonomy, prefix sections, token
budget, retention, tightened rules — is yours to declare; the full boundary is in
[`docs/adapter-contract.md`](./docs/adapter-contract.md).

## Operator verbs

The person the memory is *about* gets first-class controls:

```bash
memento --store ./memento status
memento --store ./memento view profile.md
memento --store ./memento history profile.md
memento --store ./memento rollback profile.md
memento --store ./memento edit profile.md
memento --store ./memento forget languages/de --adapter-file ./adapter.json
memento --store ./memento recall kites --since 2026-07-01T00:00:00Z --budget 400
memento --store ./memento backup --remote git@github.com:you/private-store.git --yes
```

## Store layout

```
<store_root>/
  profile.md  interests.md      projected documents
  errors/fr.jsonl               event streams
  sessions/log-*.md             free-prose session logs
  .memento/                     engine area
    schema_version  facts.json  documents.jsonl  tombstones.jsonl
    objects/  locks/
  .queue/                       journals, pending log, claims
```

Plain files. Git-ignore the store with a **root-anchored** pattern (`/memento/`, never
`memento/` — the bare form matches a source package of that name at any depth).

## Prior art

MEMENTO is not an implementation of a paper. It is an engineering response to a field, and the
founding ADR records which work moved which decision.

**Shaped the design:**

- [MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560) (Packer et al.,
  2023) — the two-tier shape: a small always-resident core plus a larger archive paged in on
  demand. MEMENTO's budgeted core prefix + selective recall is this idea without the OS metaphor.
- [Continuum Memory Architectures for Long-Horizon LLM Agents](https://arxiv.org/abs/2601.09913)
  (Logan, 2026) — memory has to be *written, updated, and chained*, not merely retrieved. The
  reason the write path here is a first-class gated pipeline rather than an embedding upsert.
- [MemSyco-Bench: Benchmarking Sycophancy in Agent Memory](https://arxiv.org/abs/2607.01071)
  (Xiang et al., 2026) — names the failure mode this engine exists to prevent: retrieved memory
  bending an agent toward the user and away from accuracy. Our defense is that the model never
  decides what gets written. Deterministic gates do.
- [ATANT: An Evaluation Framework for AI Continuity](https://arxiv.org/abs/2604.06710)
  (Tanguturi, 2026) — continuity measured without cross-contamination between narratives.
  Motivated an eval harness that judges **without an LLM in the loop**.

**Considered, deliberately deferred** — promising and data-hungry; revisit when there is enough
history to abstract over:

- [TiMem: Temporal-Hierarchical Memory Consolidation](https://arxiv.org/abs/2601.02845) (Li et
  al., 2026) · [RecMem: Recurrence-based Memory Consolidation](https://arxiv.org/abs/2605.16045)
  (Dai et al., 2026) · [Human-Inspired Memory Architecture for LLM
  Agents](https://arxiv.org/abs/2605.08538) (Kerestecioglu et al., 2026)

RecMem's "consolidate on recurrence, not on every interaction" is close kin to our deferred
drain; the sleep-phase framing in the human-inspired line presumes an always-on server, which is
exactly the premise MEMENTO does not accept — hence deferred, non-blocking drains and no daemon.

**Not adopted:** Mem0, Zep/Graphiti, and Letta wholesale — hosted or heavier, data custody
outside user-owned plain files, vector/graph-first for a scale we do not have. The store
interface stays shaped so one could be mounted as a backend later.

## Status

The engine is built, gated by a deterministic test tier in CI, and in production use by both
consumer classes (a Python application and shell-only agents). The API is provisional — pin by
SHA; no PyPI release yet.

## License

Open source — [AGPL-3.0](LICENSE). Commercial — contact ahoy@42labs.io.

---
If it earned its keep, [coffee is appreciated](https://buymeacoffee.com/42piratas). ☕
