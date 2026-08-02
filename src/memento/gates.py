"""The deterministic write gates (ADR D3.1) — the anti-sycophancy mechanism.

Consolidation output is accepted **all-or-nothing**. Provenance guards nothing by itself, because
the same model writes it; these gates are the defense.

Three layers, applied in order:

1. **Schema** — the proposal is shaped the way the adapter declared.
2. **Derived identity** — anything the engine can re-derive is re-derived and compared, so the model
   cannot rename an entry into a new one and quietly orphan the old.
3. **Monotonicity / anti-erosion floor** — engine-mandatory, non-disableable. Sets shrink only by
   tombstone; ordered scales move at most one step.

Adapters *tighten* by adding rules. There is no mechanism to remove a floor rule: `RuleSet.all()`
composes the floor first and the adapter's rules after, and an adapter that declares nothing still
gets the floor.

**The floor fails closed rather than skipping.** Anything it cannot verify — a list whose members
carry no identity it recognises, two members that resolve to the same identity, a collection that
changed kind under it — is a violation, not a pass. A check that silently declines to run is worse
than no check, because it reads as a green light.

Facts paths are handled as **tuples of keys**, never as dotted strings. A key containing a `.` is
ordinary in real data (`node.js`, `pt.br`, `v1.2`) and string paths silently mis-split on it, which
made identical facts look like a deletion and locked the store. Dotted form survives only for
display and for adapter-declared paths, where the dot is the documented separator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .errors import GateFailure

# Field names a list member may use to identify itself. An adapter with a different convention
# declares its own; it may add to this set, never shrink below "something identifies each member".
DEFAULT_IDENTITY_KEYS: tuple[str, ...] = ("id", "topic", "name")

#: How deep a facts tree may nest. Beyond this the walk stops and reports, rather than recursing
#: into a `RecursionError` — which is not a `MementoError`, so callers cannot catch it as one, and
#: the drain would turn it into a permanent deferral.
MAX_FACTS_DEPTH = 64

Path = tuple[str, ...]


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    detail: str

    def render(self) -> str:
        return f"[{self.rule}] {self.path}: {self.detail}"


@dataclass
class Proposal:
    """What a consolidation wants to write. Structured, because gates cannot check prose.

    `facts` is the adapter-shaped structured state the projected documents render from; `documents`
    is that rendering. Keeping both means the gates check facts and the store writes markdown.
    """

    facts: dict[str, Any] = field(default_factory=dict)
    entries: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    documents: dict[str, str] = field(default_factory=dict)
    tombstones: set[str] = field(default_factory=set)
    session_log: str | None = None


@dataclass
class StoreState:
    """The current state a proposal is judged against."""

    facts: dict[str, Any] = field(default_factory=dict)
    tombstones: set[str] = field(default_factory=set)


class Rule(Protocol):
    name: str

    def check(self, current: StoreState, proposal: Proposal) -> list[Violation]: ...


# ------------------------------------------------------------------ paths


def render_path(path: Path) -> str:
    return ".".join(path) if path else "<root>"


def _escape(segment: str) -> str:
    """Escape the separator inside a key so two different paths cannot render the same marker.

    Without this, ``("a.b", "c")`` and ``("a", "b", "c")`` both rendered ``a.b/c`` — one tombstone
    authorising a deletion at a path the operator never named. Dotted keys are ordinary data here,
    so the collision condition was the documented normal case rather than an edge one.
    """
    return segment.replace("\\", "\\\\").replace(".", "\\.")


def _unescape(segment: str) -> str:
    out, escaped = [], False
    for ch in segment:
        if escaped:
            out.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        else:
            out.append(ch)
    return "".join(out)


def path_marker(path: Path) -> str:
    """The tombstone marker that retires the node at `path`: `parent.path/key`."""
    if not path:
        return ""
    if len(path) == 1:
        return path[-1]
    return f"{'.'.join(_escape(p) for p in path[:-1])}/{path[-1]}"


def split_marker(marker: str) -> tuple[Path, str]:
    """Split a `parent.path/key` marker back into segments, honouring escaped separators.

    The inverse of `path_marker`, so the thing the gate reports, the thing the operator forgets, and
    the thing this resolves are the same identifier even when a key contains a dot.
    """
    parents, _, key = marker.rpartition("/")
    if not parents:
        return (), key
    segments, current, escaped = [], [], False
    for ch in parents:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ".":
            segments.append("".join(current))
            current = []
        else:
            current.append(ch)
    segments.append("".join(current))
    return tuple(s for s in segments if s != ""), key


def parse_declared(path: str) -> Path:
    """An adapter-declared path. Here — and only here — `.` is the segment separator."""
    return tuple(p for p in path.split(".") if p != "")


def member_key(item: Any, identity_keys: Sequence[str] = DEFAULT_IDENTITY_KEYS) -> str | None:
    """The identity of a collection member, or None when it has none the engine can see.

    List members are addressed by their own identity, never by position: positional addressing makes
    a reordered list read as a wholesale replacement, so every rule fires on a no-op and none fires
    on a real substitution. Returning None is how "I cannot address this" reaches the floor, which
    turns it into a violation rather than a silent skip.
    """
    if isinstance(item, Mapping):
        # Engine keys are consulted first, always. An adapter widens this set to describe a taxonomy
        # the engine cannot guess — but a *superset* that happened to be searched first could put a
        # non-identity field (`engagement`) ahead of a real one (`topic`) and blind the floor to
        # substitution. Adding a key may only help; it may never displace.
        for key in (*DEFAULT_IDENTITY_KEYS, *identity_keys):
            value = item.get(key)
            if value is not None:
                return str(value)
        return None
    if isinstance(item, (str, int, float, bool)):
        return str(item)
    return None


@dataclass(frozen=True)
class Membership:
    kind: str  # "mapping" | "sequence"
    members: dict[str, Any]
    problem: str | None = None


def membership(value: Any, identity_keys: Sequence[str] = DEFAULT_IDENTITY_KEYS) -> Membership | None:
    """Membership view of a collection, or None if the value is not one."""
    if isinstance(value, Mapping):
        return Membership("mapping", {str(k): v for k, v in value.items()})
    if isinstance(value, (list, tuple, set)):
        members: dict[str, Any] = {}
        for i, item in enumerate(value):
            key = member_key(item, identity_keys)
            if key is None:
                return Membership(
                    "sequence",
                    {},
                    problem=(
                        f"member {i} carries no identity the engine recognises "
                        f"(looked for {', '.join(identity_keys)}) — declare one on the adapter, "
                        "or the floor cannot tell a substitution from a reorder"
                    ),
                )
            if key in members:
                return Membership(
                    "sequence",
                    {},
                    problem=f"two members resolve to the same identity {key!r}",
                )
            members[key] = item
        return Membership("sequence", members)
    return None


class TooDeep(Exception):
    """Raised through `walk` when a facts tree nests past `MAX_FACTS_DEPTH`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(render_path(path))


def walk(
    obj: Any, identity_keys: Sequence[str] = DEFAULT_IDENTITY_KEYS, path: Path = ()
) -> Iterable[tuple[Path, Any]]:
    """Yield every (path, value) in a facts tree, addressing list members by identity.

    Descent stops at a collection the engine cannot address; the floor reports that node instead of
    guessing its way further down. Depth is bounded for the same reason — an unbounded walk turns a
    deeply nested proposal into a `RecursionError`, which is not something a caller can catch as a
    store error.
    """
    if len(path) > MAX_FACTS_DEPTH:
        raise TooDeep(path)
    yield path, obj
    view = membership(obj, identity_keys)
    if view is None or view.problem is not None:
        return
    for key, value in view.members.items():
        yield from walk(value, identity_keys, path + (key,))


def _step(node: Any, part: str, identity_keys: Sequence[str]) -> tuple[bool, Any]:
    if isinstance(node, Mapping):
        return (True, node[part]) if part in node else (False, None)
    if isinstance(node, (list, tuple)):
        for item in node:
            if member_key(item, identity_keys) == part:
                return True, item
    return False, None


def get(
    obj: Any, path: Path, identity_keys: Sequence[str] = DEFAULT_IDENTITY_KEYS
) -> tuple[bool, Any]:
    """Look up a path, with `*` matching any single member at that level."""
    if not path:
        return True, obj
    cursor: list[Any] = [obj]
    for part in path:
        nxt: list[Any] = []
        for node in cursor:
            if part == "*":
                if isinstance(node, Mapping):
                    nxt.extend(node.values())
                elif isinstance(node, (list, tuple)):
                    nxt.extend(node)
                continue
            found, value = _step(node, part, identity_keys)
            if found:
                nxt.append(value)
        cursor = nxt
        if not cursor:
            return False, None
    return True, cursor[0] if len(cursor) == 1 else cursor


def expand(
    obj: Any, path: Path, identity_keys: Sequence[str] = DEFAULT_IDENTITY_KEYS
) -> list[tuple[Path, Any]]:
    """Expand a `*`-bearing path into every concrete (path, value) it matches."""
    if "*" not in path:
        found, value = get(obj, path, identity_keys)
        return [(path, value)] if found else []

    cut = path.index("*")
    head, tail = path[:cut], path[cut + 1 :]
    found, node = get(obj, head, identity_keys)
    if not found:
        return []

    view = membership(node, identity_keys)
    if view is None or view.problem is not None:
        return []

    out: list[tuple[Path, Any]] = []
    for key, value in view.members.items():
        concrete = head + (key,)
        if tail:
            out.extend((concrete + sub, val) for sub, val in expand(value, tail, identity_keys))
        else:
            out.append((concrete, value))
    return out


def _covered(path: Path, allowed: set[str]) -> bool:
    """True when `path` — or an ancestor of it — has been tombstoned.

    Ancestors count because retiring a language retires what is inside it. Nothing else counts: a
    marker matches a full path or it matches nothing, so forgetting a top-level `de` cannot
    authorize dropping `contacts.de` on the other side of the tree.
    """
    if not path:
        return False
    return any(path_marker(path[:i]) in allowed for i in range(1, len(path) + 1))


# --------------------------------------------------------------------- schema


@dataclass(frozen=True)
class FieldSpec:
    """One field's constraints.

    `check` is the code-shaped extension point, available to an adapter written in Python.
    `pattern` is its JSON-expressible sibling — a regex *string*, so a consumer that declares its
    adapter in a spec file can constrain a field's text without shipping a callable. Both tighten;
    neither can widen anything, because a field with no spec at all is already unconstrained.
    """

    type: type | tuple[type, ...] | None = None
    required: bool = False
    enum: Sequence[Any] | None = None
    check: Callable[[Any], bool] | None = None
    pattern: str | None = None


def field_violations(spec: FieldSpec, value: Any, rule: str, where: str) -> list[Violation]:
    """Apply one field spec to one value. Shared so the facts tree and event entries agree.

    Type is checked first and short-circuits: `enum`, `pattern` and `check` all assume they were
    handed the kind of value they were written for, and reporting four consequences of one wrong
    type reads as four defects.
    """
    if spec.type is not None and not isinstance(value, spec.type):
        return [Violation(rule, where, f"expected {spec.type}, got {type(value).__name__}")]
    out: list[Violation] = []
    if spec.enum is not None and value not in spec.enum:
        out.append(Violation(rule, where, f"{value!r} is not one of {list(spec.enum)}"))
    if spec.pattern is not None:
        if not isinstance(value, str):
            out.append(
                Violation(rule, where, f"pattern {spec.pattern!r} needs text, got {type(value).__name__}")
            )
        elif re.search(spec.pattern, value) is None:
            out.append(Violation(rule, where, f"{value!r} does not match {spec.pattern!r}"))
    if spec.check is not None and not spec.check(value):
        out.append(Violation(rule, where, f"{value!r} failed the field check"))
    return out


class SchemaRule:
    """Adapter-declared field specs over the facts tree. Paths may contain `*`."""

    name = "schema"

    def __init__(
        self,
        spec: Mapping[str, FieldSpec],
        *,
        identity_keys: Sequence[str] = DEFAULT_IDENTITY_KEYS,
    ) -> None:
        self.spec = dict(spec)
        self.identity_keys = tuple(identity_keys)

    def check(self, current: StoreState, proposal: Proposal) -> list[Violation]:
        out: list[Violation] = []
        for declared, spec in self.spec.items():
            matches = expand(proposal.facts, parse_declared(declared), self.identity_keys)
            if not matches:
                if spec.required:
                    out.append(Violation(self.name, declared, "required field is missing"))
                continue
            for path, value in matches:
                out.extend(field_violations(spec, value, self.name, render_path(path)))
        return out


class EntrySchemaRule:
    """The same specs, applied to every proposed event-log entry."""

    name = "entry-schema"

    def __init__(self, spec: Mapping[str, FieldSpec]) -> None:
        self.spec = dict(spec)

    def check(self, current: StoreState, proposal: Proposal) -> list[Violation]:
        out: list[Violation] = []
        for stream, entries in proposal.entries.items():
            for i, entry in enumerate(entries):
                where = f"{stream}[{i}]"
                if not isinstance(entry, Mapping):
                    out.append(Violation(self.name, where, "entry is not an object"))
                    continue
                for key, spec in self.spec.items():
                    if key not in entry:
                        if spec.required:
                            out.append(Violation(self.name, f"{where}.{key}", "required"))
                        continue
                    out.extend(
                        field_violations(spec, entry[key], self.name, f"{where}.{key}")
                    )
        return out


# ----------------------------------------------------------- derived identity


class DerivedIdentityRule:
    """Re-derive what the engine can derive, and compare.

    `entry_id` catches the failure this rule exists for: a model that renames an entry creates a
    second entry and orphans the first, which reads as growth and is actually erosion.
    """

    name = "derived-identity"

    def __init__(
        self,
        *,
        entry_id: Callable[[str, Mapping[str, Any]], str | None] | None = None,
        facts: Mapping[str, Callable[[dict[str, Any]], Any]] | None = None,
        identity_keys: Sequence[str] = DEFAULT_IDENTITY_KEYS,
    ) -> None:
        self.entry_id = entry_id
        self.facts = dict(facts or {})
        self.identity_keys = tuple(identity_keys)

    def check(self, current: StoreState, proposal: Proposal) -> list[Violation]:
        out: list[Violation] = []
        if self.entry_id is not None:
            for stream, entries in proposal.entries.items():
                for i, entry in enumerate(entries):
                    expected = self.entry_id(stream, entry)
                    if expected is None:
                        continue
                    actual = entry.get("id")
                    if actual != expected:
                        out.append(
                            Violation(
                                self.name,
                                f"{stream}[{i}].id",
                                f"derives to {expected!r}, proposal says {actual!r}",
                            )
                        )
        for declared, derive in self.facts.items():
            found, value = get(proposal.facts, parse_declared(declared), self.identity_keys)
            expected = derive(proposal.facts)
            if not found or value != expected:
                out.append(
                    Violation(self.name, declared, f"derives to {expected!r}, proposal says {value!r}")
                )
        return out


# ------------------------------------------------------- the anti-erosion floor


class AntiErosionFloor:
    """Engine-mandatory. Sets shrink only by tombstone.

    Structural on purpose: it reads the shape of the current facts rather than a declaration, so an
    adapter that declares nothing at all still cannot erode a store. There is no flag that turns it
    off — an adapter can only add rules on top, or widen the identity keys it uses to address
    members.
    """

    name = "anti-erosion"

    def __init__(self, identity_keys: Sequence[str] = DEFAULT_IDENTITY_KEYS) -> None:
        self.identity_keys = tuple(identity_keys)

    def check(self, current: StoreState, proposal: Proposal) -> list[Violation]:
        allowed = set(current.tombstones) | set(proposal.tombstones)
        out: list[Violation] = []

        # Check what is being *written* first. Walking only the current state let an unverifiable
        # collection land on an empty store and then fail every later proposal — including an exact
        # no-op — which, with all-or-nothing writes, locked the store for good.
        try:
            for path, value in walk(proposal.facts, self.identity_keys):
                view = membership(value, self.identity_keys)
                if view is not None and view.problem is not None:
                    out.append(
                        Violation(
                            self.name,
                            render_path(path),
                            f"cannot be stored: {view.problem}",
                        )
                    )
        except TooDeep as exc:
            return [
                Violation(
                    self.name,
                    render_path(exc.path),
                    f"nests deeper than {MAX_FACTS_DEPTH} levels; flatten it or store it as an entry",
                )
            ]
        if out:
            return out

        try:
            current_nodes = list(walk(current.facts, self.identity_keys))
        except TooDeep as exc:  # already on disk from an older build: report, do not crash
            return [
                Violation(
                    self.name,
                    render_path(exc.path),
                    f"stored facts nest deeper than {MAX_FACTS_DEPTH} levels and cannot be verified",
                )
            ]
        for path, value in current_nodes:
            if _covered(path, allowed):
                continue  # this node, or something containing it, was explicitly retired
            where = render_path(path)

            before = membership(value, self.identity_keys)
            if before is None:
                continue  # a scalar: its disappearance is caught as a missing member of its parent
            if before.problem is not None:
                out.append(Violation(self.name, where, f"cannot be verified: {before.problem}"))
                continue

            found, proposed = get(proposal.facts, path, self.identity_keys)
            if not found:
                out.append(Violation(self.name, where, "collection disappeared from the proposal"))
                continue

            after = membership(proposed, self.identity_keys)
            if after is None:
                out.append(Violation(self.name, where, "collection replaced by a non-collection"))
                continue
            if after.problem is not None:
                out.append(
                    Violation(self.name, where, f"proposal cannot be verified: {after.problem}")
                )
                continue
            if before.kind != after.kind:
                out.append(
                    Violation(
                        self.name,
                        where,
                        f"collection changed from {before.kind} to {after.kind}; "
                        "the members may survive the change but their values do not",
                    )
                )
                continue

            for key in before.members:
                if key in after.members:
                    continue
                marker = path_marker(path + (key,))
                if marker in allowed:
                    continue
                out.append(
                    Violation(
                        self.name,
                        marker,
                        "dropped without a tombstone — retire it explicitly or keep it",
                    )
                )
        return out


class RequiredMembersRule:
    """Adapter-declared members that must survive every consolidation.

    Strictly a tightening of the floor. The floor already refuses a member that disappears without a
    tombstone; this refuses one that disappears *with* one, for the handful of members a consumer
    considers structural rather than observational — an operator's own profile key, say. Declaring
    nothing here leaves the floor exactly as it was.
    """

    name = "required-members"

    def __init__(
        self,
        required: Mapping[str, Sequence[str]],
        *,
        identity_keys: Sequence[str] = DEFAULT_IDENTITY_KEYS,
    ) -> None:
        self.required = {parse_declared(k): [str(m) for m in v] for k, v in required.items()}
        self.identity_keys = tuple(identity_keys)

    def check(self, current: StoreState, proposal: Proposal) -> list[Violation]:
        out: list[Violation] = []
        for declared, members in self.required.items():
            where = render_path(declared)
            found, value = get(proposal.facts, declared, self.identity_keys)
            if not found:
                out.append(Violation(self.name, where, "declared required, and is missing"))
                continue
            view = membership(value, self.identity_keys)
            if view is None:
                out.append(Violation(self.name, where, "is not a collection; its members cannot be required"))
                continue
            if view.problem is not None:
                out.append(Violation(self.name, where, f"cannot be verified: {view.problem}"))
                continue
            for member in members:
                if member not in view.members:
                    out.append(
                        Violation(self.name, f"{where}.{member}", "declared required, and is missing")
                    )
        return out


#: The floor's own ceiling on scale movement. An adapter may declare a *smaller* step — freezing a
#: scale outright with 0 — and may never declare a larger one. Enforced twice on purpose: the spec
#: loader refuses a loosening declaration at load, and this clamp means a library caller cannot
#: loosen it either.
MAX_SCALE_STEP = 1


class OrderedScaleFloor:
    """Engine-mandatory. Declared ordered scales move at most one step per consolidation.

    A scale needs its ordering declared — the engine cannot know that A2 precedes B1 — so an adapter
    with no declared scales has nothing to check here. That is not a hole: `AntiErosionFloor` needs
    no declaration and is what makes the empty-rule-set case fail closed.
    """

    name = "ordered-scale"

    def __init__(
        self,
        scales: Mapping[str, Sequence[Any]] | None = None,
        *,
        identity_keys: Sequence[str] = DEFAULT_IDENTITY_KEYS,
        max_steps: Mapping[str, int] | None = None,
    ) -> None:
        self.scales = {parse_declared(k): list(v) for k, v in (scales or {}).items()}
        self.max_steps = {
            parse_declared(k): min(int(v), MAX_SCALE_STEP) for k, v in (max_steps or {}).items()
        }
        self.identity_keys = tuple(identity_keys)

    def limit_for(self, declared: Path) -> int:
        return self.max_steps.get(declared, MAX_SCALE_STEP)

    def check(self, current: StoreState, proposal: Proposal) -> list[Violation]:
        out: list[Violation] = []
        for declared, order in self.scales.items():
            limit = self.limit_for(declared)
            for path, new_value in expand(proposal.facts, declared, self.identity_keys):
                where = render_path(path)
                if new_value not in order:
                    out.append(Violation(self.name, where, f"{new_value!r} is not on the declared scale"))
                    continue
                found, old_value = get(current.facts, path, self.identity_keys)
                if not found or old_value not in order:
                    continue  # a new member, or a value that was never on the scale
                step = abs(order.index(new_value) - order.index(old_value))
                if step > limit:
                    allowed = "no movement" if limit == 0 else f"at most {limit}"
                    out.append(
                        Violation(
                            self.name,
                            where,
                            f"{old_value!r} → {new_value!r} is {step} steps; "
                            f"{allowed} per consolidation",
                        )
                    )
        return out


class NoEntryRewrite:
    """Engine-mandatory. Proposals append; they never rewrite or drop existing events."""

    name = "append-only"

    def check(self, current: StoreState, proposal: Proposal) -> list[Violation]:
        out: list[Violation] = []
        for stream, entries in proposal.entries.items():
            for i, entry in enumerate(entries):
                if isinstance(entry, Mapping) and entry.get("event") in {"delete", "remove"}:
                    out.append(
                        Violation(
                            self.name,
                            f"{stream}[{i}]",
                            "deletion is not an event; retire the entry instead",
                        )
                    )
        return out


class RuleSet:
    """The floor, plus whatever the adapter adds. The floor cannot be removed."""

    def __init__(
        self,
        adapter_rules: Sequence[Rule] = (),
        *,
        ordered_scales: Mapping[str, Sequence[Any]] | None = None,
        identity_keys: Sequence[str] = DEFAULT_IDENTITY_KEYS,
        ordered_scale_steps: Mapping[str, int] | None = None,
    ) -> None:
        keys = tuple(identity_keys)
        self.identity_keys = keys
        self.floor: list[Rule] = [
            AntiErosionFloor(keys),
            OrderedScaleFloor(ordered_scales, identity_keys=keys, max_steps=ordered_scale_steps),
            NoEntryRewrite(),
        ]
        self.adapter_rules = list(adapter_rules)

    def all(self) -> list[Rule]:
        return [*self.floor, *self.adapter_rules]

    def check(self, current: StoreState, proposal: Proposal) -> list[Violation]:
        out: list[Violation] = []
        for rule in self.all():
            out.extend(rule.check(current, proposal))
        return out

    def enforce(self, current: StoreState, proposal: Proposal) -> None:
        violations = self.check(current, proposal)
        if violations:
            raise GateFailure(violations)
