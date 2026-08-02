"""Declarative adapters — an adapter a consumer *declares* instead of importing (ADR D1).

Every consumer so far has been a Python application, so `Adapter` was always constructed in code:
`render_documents` is a callable, `token_counter` is an object, rules are classes. That is the right
shape for an app. It is unusable for a consumer that has no Python of its own — an agent driven by
markdown and a shell, which is the second consumer class 42labs actually has.

So this module builds the same `Adapter` from a JSON file. Nothing here loosens the engine: the
declared adapter goes through `Adapter.rule_set()` like any other, so the anti-erosion floor and the
secrets gate apply to it exactly as they apply to jubs. What is *not* offered is arbitrary code —
a declarative consumer cannot ship a custom `Rule`, and it renders its documents with the renderer
below rather than one of its own.

The renderer is the whole reason this file can exist. `render_documents` must be a pure,
deterministic function of facts (`docs/adapter-contract.md`): same facts, same bytes. A declarative
consumer cannot supply that, so the engine supplies one — mappings sorted by key, list members
sorted by the identity the floor addresses them with, so the bytes are a function of the *content*
and never of dictionary insertion order.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adapter import Adapter, PrefixSection
from .errors import MementoError
from .gates import DEFAULT_IDENTITY_KEYS, MAX_SCALE_STEP, FieldSpec, member_key
from .queue import RetentionPolicy
from .tokenizer import HeuristicCounter

#: JSON has no types, so a spec names them. Anything outside this map is refused rather than
#: guessed — a misspelled type that silently means "unchecked" is a gate that reads as present.
TYPES: dict[str, type | tuple[type, ...]] = {
    "str": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "list": list,
    "dict": dict,
}


# --------------------------------------------------------------------- rendering


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _sorted_members(value: Sequence[Any], identity_keys: Sequence[str]) -> list[Any]:
    """List members in identity order.

    Sorted, not as-given: the gates address members by identity and treat a reorder as a no-op, so
    rendering in input order would emit different bytes for facts the engine considers identical —
    a `document_replaced` event on every consolidation and an unreadable history.
    """
    return sorted(value, key=lambda item: (member_key(item, identity_keys) or "", _scalar(item)))


def _identity_field(item: Mapping[str, Any], identity_keys: Sequence[str]) -> str | None:
    """*Which* key identifies this member — not its value, which is what `member_key` returns.

    The renderer needs the name so it can drop that one field from the member's body, having already
    printed it as the member's label. Dropping by *value* instead loses any other field that happens
    to carry the same string, and a lossy renderer cannot be inverted (T5).
    """
    for key in (*DEFAULT_IDENTITY_KEYS, *identity_keys):
        if item.get(key) is not None:
            return key
    return None


def _render_value(value: Any, identity_keys: Sequence[str], depth: int = 0) -> list[str]:
    indent = "  " * depth
    lines: list[str] = []
    if isinstance(value, Mapping):
        for key in sorted(value, key=str):
            inner = value[key]
            if isinstance(inner, (Mapping, list, tuple)):
                lines.append(f"{indent}- **{key}**")
                lines.extend(_render_value(inner, identity_keys, depth + 1))
            else:
                lines.append(f"{indent}- **{key}**: {_scalar(inner)}")
        return lines
    if isinstance(value, (list, tuple)):
        for item in _sorted_members(list(value), identity_keys):
            if isinstance(item, Mapping):
                label = member_key(item, identity_keys) or ""
                identity = _identity_field(item, identity_keys)
                rest = {k: v for k, v in item.items() if k != identity}
                lines.append(f"{indent}- **{label}**" if label else f"{indent}-")
                if rest:
                    lines.extend(_render_value(rest, identity_keys, depth + 1))
            else:
                lines.append(f"{indent}- {_scalar(item)}")
        return lines
    lines.append(f"{indent}{_scalar(value)}")
    return lines


def render_documents_from_spec(
    documents: Mapping[str, Mapping[str, Any]], identity_keys: Sequence[str]
) -> Any:
    """Build the `render_documents` callable a declared adapter uses.

    A document declares which top-level facts keys it covers. A document whose keys are all absent
    renders to nothing and is omitted, so an empty store does not accumulate empty files.
    """

    def render(facts: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in sorted(documents):
            declared = documents[name]
            title = declared.get("title", Path(name).stem.replace("-", " ").title())
            sections = list(declared.get("sections", []))
            body: list[str] = []
            for key in sections:
                if key not in facts:
                    continue
                body.append(f"## {key}")
                body.extend(_render_value(facts[key], identity_keys))
                body.append("")
            if not body:
                continue
            out[name] = "\n".join([f"# {title}", "", *body]).rstrip() + "\n"
        return out

    return render


# ------------------------------------------------------------- the inverse parser


#: One rendered bullet: `- **key**: value`, `- **key**`, `- value`, or a bare `-`.
_BULLET = re.compile(
    r"^(?P<indent>(?:  )*)-(?:\s\*\*(?P<key>.+?)\*\*(?::\s(?P<val>.*))?|\s(?P<plain>.+)|\s*)$"
)
_HEADING = re.compile(r"^##\s+(?P<key>.+?)\s*$")


@dataclass
class _Bullet:
    label: str | None  # None when the member carries no identity of its own
    value: str | None  # None when the bullet opens a nested block
    children: list["_Bullet"] = dataclass_field(default_factory=list)


def _parse_bullets(lines: Sequence[str]) -> list[_Bullet]:
    """Rebuild the indent tree the renderer flattened. Two spaces per level, as it emits."""
    roots: list[_Bullet] = []
    stack: list[tuple[int, _Bullet]] = []
    for line in lines:
        match = _BULLET.match(line)
        if match is None:
            continue  # not a bullet: prose a consumer added by hand, left where it was
        depth = len(match.group("indent")) // 2
        key, val, plain = match.group("key"), match.group("val"), match.group("plain")
        node = _Bullet(label=key, value=val if key is not None else plain)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if stack:
            stack[-1][1].children.append(node)
        else:
            roots.append(node)
        stack.append((depth, node))
    return roots


def _path_matches(declared: str, path: tuple[str, ...]) -> bool:
    parts = declared.split(".")
    if len(parts) != len(path):
        return False
    return all(p == "*" or p == actual for p, actual in zip(parts, path))


def _declared_for(specs: Mapping[str, FieldSpec], path: tuple[str, ...]) -> FieldSpec | None:
    for declared, spec in specs.items():
        if _path_matches(declared, path):
            return spec
    return None


def _coerce(text: str, spec: FieldSpec | None) -> Any:
    """Turn a rendered scalar back into the type the spec declared.

    The renderer stringifies everything and spells booleans `yes`/`no`, so a generic byte-level
    round-trip is impossible — the types have to come from the declaration. A value that will not
    convert is left as text rather than guessed at: the schema gate then reports it as the type
    error it is, at the path it is at.
    """
    if spec is None or spec.type is None:
        return text
    if spec.type is bool:
        return {"yes": True, "no": False}.get(text, text)
    try:
        if spec.type is int:
            return int(text)
        if spec.type == (int, float):
            return float(text)
    except ValueError:
        return text
    return text


def _collection(collections: Mapping[str, Mapping[str, Any]], path: tuple[str, ...]) -> Mapping[str, Any] | None:
    for declared, declared_kind in collections.items():
        if _path_matches(declared, path):
            return declared_kind
    return None


def _rebuild(
    bullets: Sequence[_Bullet],
    path: tuple[str, ...],
    schema: Mapping[str, FieldSpec],
    collections: Mapping[str, Mapping[str, Any]],
) -> Any:
    """Turn one block of bullets back into facts, keyed to what the spec declared.

    A block whose every bullet is labelled is genuinely ambiguous — a mapping and a list of
    identified members render identically — so the spec has to say which. Undeclared means mapping,
    the renderer's own default; a consumer that stores lists of objects declares them under
    `collections` and gets them back with their identity field intact.
    """
    declared = _collection(collections, path)
    anonymous = any(b.label is None for b in bullets)
    as_list = anonymous or (declared is not None and declared.get("kind") == "list")

    if not as_list:
        out: dict[str, Any] = {}
        for bullet in bullets:
            child_path = path + (bullet.label or "",)
            if bullet.children:
                out[bullet.label or ""] = _rebuild(bullet.children, child_path, schema, collections)
            else:
                out[bullet.label or ""] = _coerce(bullet.value or "", _declared_for(schema, child_path))
        return out

    identity_key = (declared or {}).get("identity_key", "id")
    members: list[Any] = []
    for bullet in bullets:
        if bullet.label is None:
            if bullet.children:
                # A member the renderer could not label — it carries no identity the engine
                # recognises. The floor refuses to *write* one, so this only ever comes from a
                # document written by something else; reading its body back as an empty string
                # would drop it silently, and adoption is the one place that must not.
                # Descend under an empty member key, so its body is read the same way a labelled
                # member's is — `practice.*.weight` matches either. Recursing on `path` itself would
                # re-enter the *list* and turn the body's first field into a new member.
                members.append(_rebuild(bullet.children, path + ("",), schema, collections))
                continue
            child_path = path + (bullet.value or "",)
            members.append(_coerce(bullet.value or "", _declared_for(schema, child_path)))
            continue
        child_path = path + (bullet.label,)
        member = {identity_key: bullet.label}
        if bullet.children:
            rest = _rebuild(bullet.children, child_path, schema, collections)
            if isinstance(rest, Mapping):
                member.update(rest)
        members.append(member)
    return members


def facts_from_documents(
    store: Any,
    documents: Mapping[str, Mapping[str, Any]],
    *,
    schema: Mapping[str, FieldSpec],
    collections: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Read the projected documents back into facts — the inverse of the declared renderer.

    This is what gives a declared adapter an anti-erosion baseline on a store that predates
    `.memento/facts.json`: without it the first consolidation is judged against an empty state, and
    an empty state cannot be eroded. Type-directed, because the renderer is lossy about types; and
    keyed to the declared documents, because a document nobody declared is nobody's facts.
    """
    facts: dict[str, Any] = {}
    for name in sorted(documents):
        content = store.read_document(name)
        if not content:
            continue
        sections = list(documents[name].get("sections", []))
        current: str | None = None
        buffered: dict[str, list[str]] = {}
        for line in content.splitlines():
            heading = _HEADING.match(line)
            if heading:
                current = heading.group("key")
                buffered.setdefault(current, [])
                continue
            if current is not None:
                buffered[current].append(line)
        for key, lines in buffered.items():
            if key not in sections:
                continue  # a section this document does not declare is not this adapter's to read
            bullets = _parse_bullets(lines)
            if bullets:
                facts[key] = _rebuild(bullets, (key,), schema, collections)
    return facts


# ------------------------------------------------------------------------ specs


#: What a declared field spec may say. `check` is deliberately absent: it is a callable, and a spec
#: file stays code-free. `pattern` is its declarative stand-in.
FIELD_SPEC_KEYS = {"type", "required", "enum", "pattern"}


def _field_spec(raw: Mapping[str, Any], where: str) -> FieldSpec:
    if not isinstance(raw, Mapping):
        raise MementoError(f"{where}: a field spec must be an object")
    unknown = sorted(set(raw) - FIELD_SPEC_KEYS)
    if unknown:
        # `requried: true` would otherwise be silently ignored, and the field would read as
        # constrained while being checked by nothing at all.
        raise MementoError(
            f"{where}: unknown field spec key(s): {', '.join(unknown)}; "
            f"expected one of {', '.join(sorted(FIELD_SPEC_KEYS))}"
        )
    declared = raw.get("type")
    if declared is not None and declared not in TYPES:
        raise MementoError(
            f"{where}: unknown type {declared!r}; expected one of {', '.join(sorted(TYPES))}"
        )
    pattern = raw.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise MementoError(f"{where}: pattern must be a regex string")
        try:
            re.compile(pattern)
        except re.error as exc:
            # A pattern that cannot compile would raise from inside the gate, mid-consolidation,
            # where it reads as an engine fault rather than as the spec error it is.
            raise MementoError(f"{where}: pattern {pattern!r} is not a valid regex ({exc})") from exc
    return FieldSpec(
        type=TYPES[declared] if declared is not None else None,
        required=bool(raw.get("required", False)),
        enum=list(raw["enum"]) if raw.get("enum") is not None else None,
        pattern=pattern,
    )


def _ordered_scales(raw: Mapping[str, Any]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for path, order in raw.items():
        if not isinstance(order, (list, tuple)) or not order:
            raise MementoError(f"ordered_scales.{path}: must be a non-empty list, in order")
        values = list(order)
        seen = {json.dumps(v, sort_keys=True) for v in values}
        if len(seen) != len(values):
            # `list.index` returns the first match, so a repeated value makes a real multi-step jump
            # measure as fewer steps than it is — a loosening, spelled as a typo.
            raise MementoError(f"ordered_scales.{path}: repeats a value, so a step cannot be measured")
        out[path] = values
    return out


def _scale_steps(raw: Mapping[str, Any], scales: Mapping[str, Any]) -> dict[str, int]:
    """Per-scale movement limits. Tightening only — the floor's own limit is the ceiling."""
    out: dict[str, int] = {}
    for path, value in raw.items():
        if path not in scales:
            raise MementoError(
                f"ordered_scale_steps.{path}: no ordered scale is declared at that path"
            )
        if not isinstance(value, int) or isinstance(value, bool):
            raise MementoError(f"ordered_scale_steps.{path}: must be an integer")
        if value < 0:
            raise MementoError(f"ordered_scale_steps.{path}: cannot be negative")
        if value > MAX_SCALE_STEP:
            raise MementoError(
                f"ordered_scale_steps.{path}: {value} would loosen the floor's limit of "
                f"{MAX_SCALE_STEP}; a spec may tighten a gate, never widen one"
            )
        out[path] = value
    return out


#: Collection kinds a spec may declare, so the inverse parser can tell a mapping from a list of
#: identified members — the one thing the rendered markdown genuinely cannot say for itself.
COLLECTION_KEYS = {"kind", "identity_key"}
COLLECTION_KINDS = {"mapping", "list"}


def _collections(raw: Mapping[str, Any], identity_keys: Sequence[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path, declared in raw.items():
        if not isinstance(declared, Mapping):
            raise MementoError(f"collections.{path}: must be an object")
        unknown = sorted(set(declared) - COLLECTION_KEYS)
        if unknown:
            raise MementoError(f"collections.{path}: unknown key(s): {', '.join(unknown)}")
        kind = declared.get("kind", "mapping")
        if kind not in COLLECTION_KINDS:
            raise MementoError(
                f"collections.{path}: unknown kind {kind!r}; expected one of "
                f"{', '.join(sorted(COLLECTION_KINDS))}"
            )
        entry: dict[str, Any] = {"kind": kind}
        if kind == "list":
            identity = declared.get("identity_key")
            if identity is None:
                raise MementoError(
                    f"collections.{path}: a list must name its identity_key, or its members "
                    "cannot be read back out of the rendered document"
                )
            if identity not in identity_keys:
                # The floor addresses members by the adapter's identity keys. An identity_key it
                # does not recognise reads back facts the floor then cannot verify at all.
                raise MementoError(
                    f"collections.{path}: identity_key {identity!r} is not one of this adapter's "
                    f"identity_keys ({', '.join(identity_keys)})"
                )
            entry["identity_key"] = identity
        out[path] = entry
    return out


def _required_members(raw: Mapping[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path, members in raw.items():
        if not isinstance(members, (list, tuple)) or not members:
            raise MementoError(f"required_members.{path}: must be a non-empty list of member ids")
        if any(not isinstance(m, str) for m in members):
            raise MementoError(f"required_members.{path}: member ids must be strings")
        out[path] = [str(m) for m in members]
    return out


def adapter_from_spec(spec: Mapping[str, Any]) -> Adapter:
    """Build an `Adapter` from a declared spec. Unknown keys are refused, never ignored."""
    known = {
        "name",
        "prefix_budget_tokens",
        "recall_limit",
        "identity_keys",
        "documents",
        "prefix_sections",
        "schema",
        "entry_schema",
        "ordered_scales",
        "ordered_scale_steps",
        "required_members",
        "collections",
        "recall_budget_tokens",
        "retention",
        "distillation_prompt",
    }
    unknown = sorted(set(spec) - known)
    if unknown:
        # A typo in a spec key would otherwise disable the thing it was meant to declare, silently.
        raise MementoError(f"unknown spec key(s): {', '.join(unknown)}")
    if not spec.get("name"):
        raise MementoError("a spec must declare a name")

    identity_keys = tuple(spec.get("identity_keys", DEFAULT_IDENTITY_KEYS))
    documents = dict(spec.get("documents", {}))

    sections = []
    for raw in spec.get("prefix_sections", []):
        document = raw.get("document")
        if document is None:
            raise MementoError(f"prefix section {raw.get('name')!r} must name a document")
        sections.append(
            PrefixSection(
                name=str(raw["name"]),
                priority=int(raw.get("priority", 0)),
                render=(lambda doc: lambda store: store.read_document(doc) or "")(document),
                required=bool(raw.get("required", False)),
            )
        )

    scales = _ordered_scales(dict(spec.get("ordered_scales", {})))
    schema = {k: _field_spec(v, f"schema.{k}") for k, v in dict(spec.get("schema", {})).items()}
    collections = _collections(dict(spec.get("collections", {})), identity_keys)
    retention = dict(spec.get("retention", {}))
    return Adapter(
        name=str(spec["name"]),
        token_counter=HeuristicCounter(),
        prefix_budget_tokens=int(spec.get("prefix_budget_tokens", 2000)),
        prefix_sections=tuple(sections),
        recall_limit=int(spec.get("recall_limit", 8)),
        recall_budget_tokens=(
            int(spec["recall_budget_tokens"]) if spec.get("recall_budget_tokens") is not None else None
        ),
        schema=schema,
        entry_schema={
            k: _field_spec(v, f"entry_schema.{k}")
            for k, v in dict(spec.get("entry_schema", {})).items()
        },
        ordered_scales=scales,
        ordered_scale_steps=_scale_steps(dict(spec.get("ordered_scale_steps", {})), scales),
        required_members=_required_members(dict(spec.get("required_members", {}))),
        identity_keys=identity_keys,
        render_documents=render_documents_from_spec(documents, identity_keys),
        # The inverse of that renderer, so a declared adapter can adopt a store whose facts were
        # never written as JSON — the projected documents are the baseline the floor judges the
        # first consolidation against.
        facts_from_store=lambda store: facts_from_documents(
            store, documents, schema=schema, collections=collections
        ),
        projected_documents=tuple(sorted(documents)),
        retention=RetentionPolicy(
            keep_everything=bool(retention.get("keep_everything", True)),
            prune_after_consolidation=bool(retention.get("prune_after_consolidation", False)),
        ),
        distillation_prompt=str(spec.get("distillation_prompt", "")),
    )


def load_adapter(path: str | Path) -> Adapter:
    """Load a declared adapter from a JSON file."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MementoError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(spec, dict):
        raise MementoError(f"{path}: a spec must be a JSON object")
    return adapter_from_spec(spec)
