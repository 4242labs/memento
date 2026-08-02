# The adapter contract

The engine owns **mechanism**. The adapter owns everything **domain-shaped**. This document is the
boundary, and it is the thing a second consumer will test — until Phase C proves it, treat the API
as provisional and pin the engine by git SHA.

Authority for everything here is [`adr-260731-memento-founding.md`](../adr-260731-memento-founding.md).
Where this document and the ADR disagree, the ADR wins.

---

## What lives where

| Concern | Owner | Why |
|:--|:--|:--|
| Store format, event log, folding, locks, claims, crash recovery | **Engine** | Correctness properties that must not vary per consumer |
| The anti-erosion floor, the secrets gate | **Engine** | A floor a consumer could lower is not a floor |
| Relationship / restraint prompt templates | **Engine** | Restraint is a property of the write discipline |
| Taxonomy — which streams exist, what an entry looks like | **Adapter** | Domain-shaped |
| The distillation prompt | **Adapter** | Domain-shaped |
| Recall policy, prefix sections, token budget | **Adapter** | Depends on the serving model and the product |
| Retention policy for transcript material | **Adapter** | A stated policy, never a default the engine picks |
| The store's contents | **The operator** | Never shared, never central |

An adapter lives **inside its consumer's app repo**, not here.

---

## Minimum viable adapter

```python
from memento import Adapter, FieldSpec, PrefixSection, RetentionPolicy

LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]

ADAPTER = Adapter(
    name="jubs",
    # --- read path
    token_counter=PinnedLocalTokenizer(),        # must be local; see below
    prefix_budget_tokens=1500,
    prefix_sections=(
        PrefixSection("profile",   priority=0, render=lambda s: s.read_document("profile.md") or ""),
        PrefixSection("interests", priority=1, render=lambda s: s.read_document("interests.md") or ""),
    ),
    recall_limit=8,
    # --- write path
    schema={"languages.*.level": FieldSpec(type=str, enum=LEVELS)},
    entry_schema={"id": FieldSpec(type=str, required=True)},
    ordered_scales={"languages.*.level": LEVELS},
    derive_entry_id=lambda stream, entry: f"{stream.split('/')[-1]}-{slug(entry['pattern'])}",
    rules=(),                                     # optional: tighten further
    # --- projection
    render_documents=render_documents,            # facts -> {"profile.md": "...", ...}
    facts_from_store=parse_existing_documents,    # optional, but see "Adopting an existing store"
    # --- policy
    retention=RetentionPolicy(keep_everything=True),
    distillation_prompt=DISTILLATION_PROMPT,
)
```

---

## Declaring an adapter instead of importing one

Everything above assumes the consumer is a Python application. The other consumer class 42labs has
is an **agent** — markdown and a shell, no import statement anywhere. It declares its adapter in a
JSON file and drives the engine through the CLI:

```json
{
  "name": "clu",
  "prefix_budget_tokens": 1200,
  "identity_keys": ["id", "topic", "name", "key"],
  "documents": {
    "profile.md": {"title": "Operator", "sections": ["operator"]}
  },
  "prefix_sections": [{"name": "profile", "priority": 0, "document": "profile.md"}],
  "schema": {"operator.confidence": {"type": "str", "enum": ["low", "medium", "high"]}},
  "ordered_scales": {"operator.confidence": ["low", "medium", "high"]}
}
```

```bash
memento --store ./memento/clu prefix   --adapter-file adapter.json
memento --store ./memento/clu facts    --adapter-file adapter.json --fingerprint
memento --store ./memento/clu consolidate --adapter-file adapter.json \
        --proposal proposal.json --session 260801-1730 --expect <fingerprint>
```

**A declared adapter gets the same engine.** The spec builds an ordinary `Adapter`, so the secrets
gate, schema, ordered scales and the anti-erosion floor all apply unchanged. Exit codes are the
contract: `3` gates rejected (every violation printed), `4` secrets, `5` stale proposal. Nothing is
written on any of them.

Two things a spec cannot do, deliberately: ship a custom `Rule` (that is arbitrary code), and supply
its own `render_documents`. The engine renders instead — mappings sorted by key, list members sorted
by the identity the floor addresses them with — because a declarative consumer cannot guarantee the
determinism the contract requires and a renderer that reorders rows makes the history unreadable.

An unknown key in a spec is refused rather than ignored: a typo that silently disables the gate it
was meant to declare is worse than no gate at all.

The `--expect` fingerprint is the same compare-and-swap the library requires, carried across the
shell boundary. Read it with `facts --fingerprint`, submit with it, and a proposal derived from a
state that has since moved is refused instead of overwriting the newer one. `--unchecked` is the
deliberate opt-out, and it says so.

---

## Facts, documents, and why both exist

The projected documents are markdown, written by an LLM, and **not reconstructible from the event
log**. Gates cannot check prose. So an adapter works in two representations:

- **`facts`** — a structured dict. This is what the gates read and what the engine persists to
  `.memento/facts.json` (as a projected document, so it carries `document_replaced` history).
- **`documents`** — the markdown rendering of those facts, via `render_documents(facts)`. This is
  what the reader and the operator see.

A consolidation proposes `facts`; the engine renders and writes both, atomically, in one batch.

Facts nest at most 64 levels deep. Past that the write is refused rather than recursing — put the
detail in an event stream instead, which is what streams are for.

A crash between the `document_replaced` event and the file swap leaves a revision that was recorded
and never written. The redrive retires it with a `document_write_abandoned` event, so
`document_history` and `document_revisions` chain correctly and rollback never offers a version the
document did not hold. Pass `include_abandoned=True` to see the retired ones; nothing is deleted.

`render_documents` must be a **pure function of facts** and **deterministic** — same facts, same
bytes. Sort your collections. A renderer that reorders rows between runs produces a
`document_replaced` event on every consolidation and makes the history unreadable.

---

## The gates

Applied in order, **all-or-nothing**. Nothing is written unless everything passes.

### 1. Secrets

Engine-owned, not configurable. Documents, facts, entries, and the session log are scanned; a match
rejects the whole consolidation and defers the session. There is no "warn" mode.

**Session ids** must be a single path segment (`[A-Za-z0-9][A-Za-z0-9._-]*`). The queue and the lock
directory build paths from them directly, so anything else is refused rather than resolved.

### 2. Schema — `schema`, `entry_schema`

`FieldSpec(type=..., required=..., enum=..., check=...)` keyed by dotted path. Paths may contain
`*`, which matches any member at that level:

```
"languages.*.level"        every language's level
"interests.*.engagement"   every interest's engagement
```

### 3. Derived identity — `derive_entry_id`, `derived_facts`

Anything the engine can re-derive, it re-derives and compares. This catches the specific failure
where a model **renames** an entry: the rename creates a second entry and orphans the first, which
looks like growth on paper and is erosion in fact.

Return `None` from `derive_entry_id` for streams you do not want checked.

### 4. The anti-erosion floor — engine-mandatory

**Sets shrink only by tombstone.** Structural, and it needs no declaration from you: the floor reads
the shape of the current facts. Every collection member present in the current state must be present
in the proposal, or be tombstoned.

**It fails closed rather than skipping.** Anything the floor cannot verify is a violation, not a
pass: a list member carrying no identity it recognises, two members resolving to the same identity,
a collection that changed from a mapping to a sequence under it. A check that silently declines to
run is worse than no check, because it reads as a green light — the first build shipped exactly that
and an adapter could disable the floor by naming its identity field `lang`.

This is deliberately strict, and it includes fields:

```python
# current
{"languages": {"en": {"level": "A2", "confidence": "low", "goals": "fluency"}}}

# proposal — REJECTED: languages.en/goals dropped without a tombstone
{"languages": {"en": {"level": "A2", "confidence": "low"}}}
```

Members are addressed by **identity**, not position: a list member is keyed by its `id`, `topic`, or
`name`. Reordering a list is a no-op; only actual disappearance fires.

If your taxonomy identifies members some other way, declare it — the floor then works on your shape
instead of refusing it:

```python
ADAPTER = Adapter(name="jubs", identity_keys=("lang", "id", "topic", "name"), ...)
```

Keys may contain dots. `node.js`, `pt.br`, and `arXiv:2604.06710` are ordinary data; paths are
handled as key tuples internally, so nothing mis-splits on them. The dot is a separator only in the
paths *you* declare (`schema`, `ordered_scales`, `derived_facts`).

**An adapter that declares nothing still gets this.** That is what "an empty adapter rule set fails
closed" means, and there is no flag that disables it.

### 5. Ordered scales — `ordered_scales`

Declared scales move **at most one step per consolidation**, up or down. A scale needs its ordering
declared because the engine cannot know that `A2` precedes `B1`. Values off the declared scale are
rejected outright.

### 6. Your rules

Add `Rule` objects via `rules`. A rule is anything with `name` and
`check(current: StoreState, proposal: Proposal) -> list[Violation]`. They run **after** the floor and
can only add violations — composition is the only extension point, by design.

### Tombstones: how a shrink is allowed

A proposal may carry `tombstones={"languages/it"}`, and the operator's `forget` writes the same
marker. The marker format is `path/key` — exactly the string the gate reports when it rejects a
drop, so the thing you are told about and the thing you retire are the same identifier.

A marker authorizes **exactly one** retirement, matched on the full path. Forgetting a top-level
`de` does not authorize dropping `contacts.de` on the other side of the tree — though it does cover
everything *inside* the thing it retires, since retiring a language retires its fields with it.

Tombstones persist. A member retired in session 4 stays retirable in session 40.

---

## The read path

### Token counting

`token_counter` must expose `name`, `is_local`, and `count(text) -> int`. The engine **refuses a
non-local counter** on the prefix path: a cache prefix has to be counted in the serving model's
units, and a network call at session start is exactly what the budget exists to protect.

Ship a pinned local tokenizer. `memento.tokenizer.HeuristicCounter` is a floor, not a
recommendation — it is deterministic and model-free, which makes it right for tests and approximate
for production.

### Prefix sections

`priority` orders assembly **and** truncation: lower is more important and is cut last. On overflow
the engine drops whole trailing lines from the lowest-priority section that does not fit, then drops
sections entirely, and **always reports what it trimmed** via a `prefix-truncated` FLAG.

Mark a section `required=True` and the engine raises `BudgetError` rather than shipping a prefix
without it. Use that sparingly; it converts a degraded read into a failed one.

### Recall

`recall(store, query, limit=...)` searches events and documents. A hit must share a term with the
query — a query matching nothing returns nothing rather than the nearest thing lying around. Retired
entries are excluded unless you ask for them.

The archive is never bulk-loaded. If you find yourself calling `recall` with a huge limit to
assemble context, that belongs in the prefix instead, under the budget.

---

## The write path, in practice

```python
queue.append_turn(session, turn, {"said": text})   # during the session
queue.close_and_enqueue(session)                   # at exit — and nothing else, ever
```

Session exit closes the journal and enqueues. No LLM call, no git, no store write. Then, later:

```python
spawn_drain(
    store_root=..., queue_root=...,
    adapter_ref="myapp.memory:ADAPTER",
    distiller_ref="myapp.memory:DISTILLER",
    gate=DrainGate(prefix_materialized=True, idle_seconds=idle),
)
```

The gate is not advisory. `spawn_drain` raises `DrainRefused` if the prefix has not been
materialized (a drain rewriting documents mid-read yields a torn composite) or the session has not
been idle long enough. Spawn-gating **reduces** overlap; it does not eliminate it, and a consumer
should measure the residual rather than assume it away.

### Your distiller

```python
class Distiller:
    def distill(self, journal: list[dict], state: StoreState, prompt: str) -> Proposal: ...
```

Anything it raises becomes a **deferral plus a FLAG**, never a crash and never a partial write —
and so does anything raised by the write that follows it. One bad session defers; it never takes the
drain down and strands the sessions queued behind it.

The call happens with **no lock held**, so a slow model cannot stall the other front-end. That
window is exactly why the write is checked against a fingerprint of the state the proposal was
derived from:

```python
state = current_state(store, adapter)
proposal = distiller.distill(journal, state, adapter.distillation_prompt)
apply_consolidation(..., expected_fingerprint=facts_fingerprint(state.facts))
```

`run_drain` does this for you. Everywhere else the argument is **required** — there is no default,
because the caller who forgets it is exactly the caller whose write gets lost. A caller with no
baseline (the first write to an empty store) passes `writepath.UNCHECKED` and says so out loud.

A stale proposal raises `StaleProposal`, defers the session, and leaves the newer state intact.

---

## Adopting an existing store

The store layout is byte-compatible with jubs' — adoption moves a path, not data. One thing needs
attention: on a store that predates the engine there is no `.memento/facts.json`, so the
anti-erosion baseline would be empty on the first consolidation, and that first consolidation could
erode freely.

`facts_from_store(store) -> dict` closes that hole by parsing your existing documents into facts.
Write it for the first consolidation, keep it afterwards — it costs nothing and it is the only thing
standing between "adoption" and "one free erosion".

Verify the round trip before you ship:

```python
assert render_documents(facts_from_store(store)) == {
    "profile.md": store.read_document("profile.md"),
    "interests.md": store.read_document("interests.md"),
}
```

If that fails, adoption is a migration, whatever it is called.

---

## Backup

Off by default and not switchable by accident:

```python
enable_backup(store, acknowledged=True, remote="git@github.com:you/private-store.git")
```

`acknowledged=True` is the operator's opt-in, and the warning shown is recorded in the config. Use a
**private** remote. `add`/`commit` run under the store lock; `pull`/`push` deliberately do not.

Each consolidation commits immediately, attributed to the session it consolidated — never batched
under a later one.

---

## Retention

`RetentionPolicy` governs the transcript material in the queue, which is a bigger personal-data pile
than the store itself. `keep_everything=True` is jubs' stated policy. Pruning requires both an
explicit policy and a consolidated marker.

"Not persisted in the memory store" never means "transient". State your policy out loud.

---

## What the engine will not do for you

- Decide your taxonomy, or write your distillation prompt.
- Guess a token budget.
- Choose a retention policy.
- Merge two stores, or serve more than one operator from one store. **The store is the namespace**;
  there is no tenancy seam to reach for.
- Delete anything. Retirement is an event; there is no function in the engine that removes one.
