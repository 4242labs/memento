# MEMENTO for agent consumers

The engine's first consumer class is a Python application. Its second is an **agent**: markdown and
a shell, no import statement anywhere. For that
consumer the CLI is not a convenience wrapper around the API. It **is** the API.

Nothing here buys a weaker engine. The gates, the secrets scan, the compare-and-swap and the drain
gate all apply through the shell exactly as they apply to a library caller. The one thing that
genuinely inverts is *who runs the model*: the drain calls a Python `Distiller`, an agent **is** the
distiller. It reads the prefix and the journal, produces the proposal itself, and submits it — which
makes it precisely the writer the gates were built to distrust.

**Declare your adapter in a file.** Agent consumers use `--adapter-file <spec.json>` only. The
code-loading `--adapter module:attribute` path is for Python consumers and is outside this contract:
it imports and executes whatever the reference names.

---

## What is promised, and what is not

Two things are contractual. One is not, and the difference is the whole reason this section exists.

| Surface | Promise |
|:--|:--|
| **The exit code** | Contractual. Branch on it. The table below is exhaustive and pinned by `tests/test_exit_codes.py` |
| **`--json`** | Contractual. Every verb below takes it. The payload is complete on its own — including any error — so you never have to read stderr to learn what happened. It always carries `ok`, which agrees with the exit code by construction |
| **Console prose** | **Not contractual.** Messages are free to be reworded, reordered, or dropped between versions. Parsing them is relying on something this project does not promise |

So: **pass `--json` and read the payload.** The human output exists for the operator reading a
terminal, and it lives in a separate module (`presentation.py`) precisely so that nothing in it can
change what a consumer depends on.

```bash
memento --store ./memento pending --queue ./memento/.queue --json
```
```json
{
  "ok": true,
  "backlog": {"breached": false, "count": 1, "message": null, "oldest_age_days": 0.4, "reason": null},
  "pending": [{"deferrals": 0, "enqueued_at": 1785680834.0, "session": "260802-140000"}]
}
```

---

## The loop

Two halves, and the split between them is the point. Session exit does the cheap half. Everything
expensive happens later, behind a gate.

### During the session

```bash
memento --store ./memento journal $SESSION --queue ./memento/.queue --text "what just happened" --json
```

Append per-turn material as you go. This is the pile a consolidation is later distilled *from*; it
is the only reason anything survives the session at all.

### At session exit — this and nothing else

```bash
memento --store ./memento enqueue $SESSION --queue ./memento/.queue --json
```

No distillation, no git, nothing slow (ADR D3.2). An exit that does real work is an exit the
operator waits on.

### Later — the consolidation loop

Next session start, or an idle moment. **In this order.**

```bash
# 1. THE GATE CHECK — MANDATORY. Never skip it, never work around a refusal.
memento --store ./memento pending --queue ./memento/.queue --gate-check \
        --idle-seconds "$IDLE" --prefix-materialized --json           # exit 7 = not yet

# 2. Claim the session. Keep the token; it is the right to release it.
TOKEN=$(memento --store ./memento claim $SESSION --json | jq -r .token)   # exit 6 = someone has it

# 3. Read: the current state, the raw material, and the compare-and-swap baseline.
memento --store ./memento prefix --adapter-file ./adapter.json --json     # .text, .tokens, .flags
memento --store ./memento journal $SESSION --queue ./memento/.queue --show --json   # .turns
FP=$(memento --store ./memento facts --fingerprint --adapter-file ./adapter.json --json | jq -r .fingerprint)

# 4. Distill. This is you. Write a proposal JSON.

# 5. Submit it through the gates. All-or-nothing.
memento --store ./memento consolidate --adapter-file ./adapter.json \
        --proposal ./proposal.json --session $SESSION \
        --queue ./memento/.queue --expect "$FP" --json                # .violations on exit 3

# 6. Back it up, if this store opted in.
memento --store ./memento commit --session $SESSION --json            # .sha, .pushed

# 7. The marker, LAST.
memento --store ./memento done $SESSION --queue ./memento/.queue --json

# 8. Give the claim back.
memento --store ./memento release $SESSION --token "$TOKEN" --json
```

Every step above takes `--json`, and every payload carries `ok`. Branch on the exit code first; read
the payload for the detail — `.violations` when the gates refuse, `.flags` when something was
truncated or deferred, `.error` for the one-line reason.

### Why the gate check is mandatory

It is the same `DrainGate` the engine applies before spawning a drain subprocess, and it exists for
two reasons that both still apply when the "subprocess" is you:

* **The read prefix must be materialized.** Rewriting the documents while a prefix reader is
  concatenating them yields a torn composite. One-session-*stale* is fine; *inconsistent* is not.
* **The session must have been idle.** `--idle-seconds` is what you observed; `--min-idle-seconds`
  (default 5) is the bar.

A refusal is exit **7**. The correct response is to try again later — never to pass
`--prefix-materialized` you did not observe, and never to lower `--min-idle-seconds` to get past it.

### Why the marker is last

`done` writes the `consolidated` marker, and it is deliberately its own verb rather than a side
effect of `consolidate`. A crash before the marker means the session is still pending and gets
re-run; a re-run consolidation is cheap, and a lost one is not. Putting the marker inside
`consolidate` would place it *before* the commit, so a crash in between would lose the backup with
no way to notice.

---

## Exit codes

Branch on these. They are the contract.

| Code | Means | Do |
|:--|:--|:--|
| `0` | fine | continue |
| `1` | usage, I/O, or a refused adoption | fix the invocation; do not retry blind |
| `2` | **malformed input** — the proposal is not JSON, or not an object | fix the file. Distinct from `3`: nothing was even parsed, let alone gated |
| `3` | **the gates rejected it** — nothing was written | read `.violations` from the payload, fix the proposal, resubmit. With `--queue`, the session is already marked deferred |
| `4` | **secrets** — a credential-shaped string tried to enter the store | never retry as-is. Remove it. The store is not the place for it |
| `5` | **stale** — the store moved while you were thinking | re-read `facts --fingerprint`, redrive the proposal against the new state. This is a normal outcome, not an error |
| `6` | **claimed** — another front-end holds this session | skip it and move on. Do not force it |
| `7` | **the drain gate refuses** — too soon | come back later |

Exit 3 and exit 5 mean opposite things. Exit 3 is *your proposal is wrong*; exit 5 is *your proposal
was fine and somebody else got there first*. Exit 2 is neither: it never reached the gates.

The codes are exhaustive and pinned — `tests/test_exit_codes.py` holds one row per scenario, and a
verb without a row is a hole that file exists not to have.

---

## The compare-and-swap

`--expect $FP` is not optional bookkeeping. Between reading the state and submitting a proposal, you
think — and another front-end may land a consolidation in that window. Without the fingerprint, your
write silently overwrites theirs.

```bash
FP=$(memento --store ./memento facts --fingerprint --adapter-file ./adapter.json)
# ... think ...
memento --store ./memento consolidate ... --expect "$FP"     # exit 5 if the store moved
```

`--unchecked` is the deliberate opt-out, for a genuinely first write to an empty store. It is
spelled out loud because the caller who forgets is exactly the caller whose write gets lost. One of
the two is required; there is no default.

---

## The claim

The claim is what stops two front-ends both paying for one consolidation. It is **not** the
`consolidated` marker and never touches it.

It is a claim *file* with a token and a TTL, not an `flock` — an `flock` dies with the process that
took it, and your consolidation spans several `memento` invocations with your own thinking in
between, so an `flock` claim would be gone before you used it.

* **Take it** with `claim`, which prints a token. Keep the token.
* **Give it back** with `release --token`. A wrong token is refused (exit 6) — releasing someone
  else's claim puts two claimants inside the critical section together.
* **It expires.** Default TTL is 3600s, `--ttl` overrides. A claim past its TTL is reclaimable by
  the next comer, so an agent that walks away mid-loop does not wedge the session forever.

Do not hold a claim across anything you cannot bound. If your loop dies, the TTL is the recovery
path — not an operator.

---

## Declaring the adapter

The spec file is JSON, and it stays code-free. Every key below is optional except `name`. **Unknown
keys are refused, never ignored** — a typo must not silently disable the gate it was meant to
declare.

```json
{
  "name": "advisor-legal",
  "prefix_budget_tokens": 900,
  "recall_limit": 8,
  "recall_budget_tokens": 400,
  "identity_keys": ["id", "topic", "name"],

  "documents": {
    "operator.md": {"title": "Operator", "sections": ["operator", "practice"]}
  },
  "prefix_sections": [
    {"name": "operator", "priority": 0, "document": "operator.md"}
  ],

  "schema": {
    "operator.confidence": {"type": "str", "enum": ["low", "medium", "high"]},
    "operator.reply_style": {"type": "str", "pattern": "^[a-z-]+$"}
  },
  "entry_schema": {"id": {"type": "str", "required": true}},

  "ordered_scales": {"operator.confidence": ["low", "medium", "high"]},
  "ordered_scale_steps": {"operator.confidence": 0},
  "required_members": {"practice": ["verify-before-asserting"]},
  "collections": {"practice": {"kind": "list", "identity_key": "topic"}},

  "retention": {"keep_everything": true},
  "distillation_prompt": "..."
}
```

### Tighten only

A declared adapter may make the gates **stricter**. It can never make them looser, and an attempt to
is a spec error refused at *load* — where you can still fix it — rather than at consolidation, where
you would find out after the model already produced something.

| Key | Tightens by |
|:--|:--|
| `schema` / `entry_schema` `pattern` | constraining a field's text with a regex string — the JSON-expressible sibling of a Python adapter's `check` callable |
| `ordered_scale_steps` | lowering how far a scale may move in one consolidation. `0` freezes it. Above the engine's limit of **1**, the spec is refused |
| `required_members` | naming members that must survive *even with a tombstone* — stricter than the floor, which accepts any explicitly retired drop |

The anti-erosion floor is underneath all of it and cannot be removed. An adapter that declares
nothing still gets it.

`required` on a `*`-bearing path means every member that *exists* carries the field — never that
the collection is non-empty. An empty or absent collection is not a violation: a new store starts
with every collection empty, and its first consolidation must be able to say so. Non-emptiness,
where it is genuinely wanted, is `required_members`' declaration.

### `collections`, and why the parser needs it

A mapping and a list of identified members render to *identical* markdown — both are labelled
bullets. So when the engine reads documents back into facts, only your declaration can tell them
apart. Undeclared means mapping, which is the renderer's own default. A `list` must name its
`identity_key`, and that key must be one of your `identity_keys`, or the floor could not address the
members it read back.

---

## Adopting a store that already exists

```bash
memento --store ./memento facts --from-store --adapter-file ./adapter.json
```

This parses the projected documents back into facts and proves the round-trip before trusting it.
It is what gives the *first* consolidation on a pre-existing store a real anti-erosion baseline —
an empty baseline cannot be eroded, so without it the first write is the least guarded one the store
will ever see.

**The bytes win.** If `render(parse(documents))` does not reproduce what is on disk, the command
FLAGs `adoption-diverged`, exits 1, and changes nothing. Re-projecting an operator's own memory to
match a renderer is the operator's call to make, never a consolidation's.

The parse is exact up to one thing: list members come back in the renderer's canonical
identity-sorted order, which the gates treat as a no-op.

---

## Recall

```bash
memento --store ./memento recall "kites" --adapter-file ./adapter.json \
        --stream vocab/fr --since 2026-07-01T00:00:00Z --budget 400 --json
```

`--limit` bounds how many hits come back. `--budget` bounds what they *cost*, which is the number
that matters when you are pasting them into your own context — ten hits over a long stream is not a
bounded amount of prompt. The budget defaults to the adapter's `recall_budget_tokens`. What the
budget cut is reported, never dropped quietly.

Filters: `--stream` and `--key` (both repeatable), `--since` / `--until` on ISO-8601 timestamps.
A date-ranged recall searches events only — a projected document is the *current* state and carries
no per-line history, so including one would date it to whenever you happened to look.

`--sessions` adds the verbatim session logs to the search (hit source `"session"`). Off by default:
the distilled store answers "what do I know", the logs answer "did we discuss X?" — and transcript
lines are chatty enough to crowd curated hits out of `--limit` and `--budget` if always included.

---

## What an agent does not get

* **`--adapter module:attribute`.** It imports and executes code. Use `--adapter-file`.
* **A promise about console text.** Prose is not contractual; `--json` and the exit code are.
* **A way to skip the gate check.** Exit 7 means later, not louder.
* **A weaker floor.** Every write goes through the same gates as a library caller's.
* **Deletion.** `forget` writes a tombstone. Nothing in this engine deletes an event.
