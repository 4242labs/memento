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
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adapter import Adapter, PrefixSection
from .errors import MementoError
from .gates import DEFAULT_IDENTITY_KEYS, FieldSpec, member_key
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
                rest = {k: v for k, v in item.items() if _scalar(v) != label}
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


# ------------------------------------------------------------------------ specs


def _field_spec(raw: Mapping[str, Any], where: str) -> FieldSpec:
    declared = raw.get("type")
    if declared is not None and declared not in TYPES:
        raise MementoError(
            f"{where}: unknown type {declared!r}; expected one of {', '.join(sorted(TYPES))}"
        )
    return FieldSpec(
        type=TYPES[declared] if declared is not None else None,
        required=bool(raw.get("required", False)),
        enum=list(raw["enum"]) if raw.get("enum") is not None else None,
    )


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

    retention = dict(spec.get("retention", {}))
    return Adapter(
        name=str(spec["name"]),
        token_counter=HeuristicCounter(),
        prefix_budget_tokens=int(spec.get("prefix_budget_tokens", 2000)),
        prefix_sections=tuple(sections),
        recall_limit=int(spec.get("recall_limit", 8)),
        schema={
            k: _field_spec(v, f"schema.{k}") for k, v in dict(spec.get("schema", {})).items()
        },
        entry_schema={
            k: _field_spec(v, f"entry_schema.{k}")
            for k, v in dict(spec.get("entry_schema", {})).items()
        },
        ordered_scales={k: list(v) for k, v in dict(spec.get("ordered_scales", {})).items()},
        identity_keys=identity_keys,
        render_documents=render_documents_from_spec(documents, identity_keys),
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
