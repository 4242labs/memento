"""The adapter contract (ADR D1).

The engine owns mechanism; the adapter owns everything domain-shaped — taxonomy, the distillation
prompt, recall policy, budgets, retention. Adapters live inside their consumer's app repo, never
here, and the store they point at is never shared.

Prompt ownership splits the same way: the engine ships **relationship/restraint** templates
(SHA-pinned, vendored, in `templates/`); the consumer owns **domain distillation** prompts.

An adapter may *tighten* the write gates by adding rules. It cannot loosen them: the anti-erosion
floor is composed in by `RuleSet` and there is no flag that removes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .gates import DEFAULT_IDENTITY_KEYS, FieldSpec, Rule, RuleSet
from .queue import RetentionPolicy
from .tokenizer import DEFAULT_COUNTER, TokenCounter


@dataclass(frozen=True)
class PrefixSection:
    """One chunk of the always-loaded core prefix.

    `priority` orders both assembly and truncation: lower numbers are more important and are the
    last to be cut. The order is declared, so truncation is deterministic rather than incidental.
    """

    name: str
    priority: int
    render: Callable[[Any], str]  # (store) -> text
    required: bool = False


@dataclass
class Adapter:
    name: str

    # --- read path
    token_counter: TokenCounter = DEFAULT_COUNTER
    prefix_budget_tokens: int = 2000
    prefix_sections: Sequence[PrefixSection] = ()
    recall_limit: int = 8
    # A ceiling on what recall may *cost*, distinct from how many hits it returns. An agent that
    # pastes recall output into its own context is spending prompt budget on it, and ten hits over
    # a long stream is not a bounded amount of text. None means bounded by count alone.
    recall_budget_tokens: int | None = None

    # --- write path
    schema: Mapping[str, FieldSpec] = field(default_factory=dict)
    entry_schema: Mapping[str, FieldSpec] = field(default_factory=dict)
    ordered_scales: Mapping[str, Sequence[Any]] = field(default_factory=dict)
    # Per-scale movement limits, and members that must survive every consolidation. Both tighten the
    # floor: a step above the engine's own is clamped back down, and declaring neither leaves the
    # floor exactly as it was.
    ordered_scale_steps: Mapping[str, int] = field(default_factory=dict)
    required_members: Mapping[str, Sequence[str]] = field(default_factory=dict)
    rules: Sequence[Rule] = ()
    # Field names the floor uses to address list members. Widen it when your taxonomy identifies
    # members some other way (`lang`, `code`, `label`) — a member the floor cannot address is a
    # violation, not a pass, so this is how you keep legitimate data legible to it.
    identity_keys: Sequence[str] = DEFAULT_IDENTITY_KEYS
    derive_entry_id: Callable[[str, Mapping[str, Any]], str | None] | None = None
    derived_facts: Mapping[str, Callable[[dict[str, Any]], Any]] = field(default_factory=dict)

    # --- projection
    render_documents: Callable[[dict[str, Any]], dict[str, str]] = lambda facts: {}
    facts_from_store: Callable[[Any], dict[str, Any]] | None = None
    # Which documents this adapter claims to project. Declaring them is what lets adoption tell "a
    # file we render nothing for" — a store this adapter cannot reproduce — from "a file that is
    # simply not ours", which is every other file under the store root.
    projected_documents: Sequence[str] = ()

    # --- policy
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    distillation_prompt: str = ""

    def rule_set(self) -> RuleSet:
        """Floor first, adapter rules after. Composition is the only extension point by design."""
        from .gates import DerivedIdentityRule, EntrySchemaRule, RequiredMembersRule, SchemaRule

        keys = tuple(self.identity_keys)
        declared: list[Rule] = []
        if self.schema:
            declared.append(SchemaRule(self.schema, identity_keys=keys))
        if self.entry_schema:
            declared.append(EntrySchemaRule(self.entry_schema))
        if self.derive_entry_id is not None or self.derived_facts:
            declared.append(
                DerivedIdentityRule(
                    entry_id=self.derive_entry_id, facts=self.derived_facts, identity_keys=keys
                )
            )
        if self.required_members:
            declared.append(RequiredMembersRule(self.required_members, identity_keys=keys))
        declared.extend(self.rules)
        return RuleSet(
            declared,
            ordered_scales=self.ordered_scales,
            identity_keys=keys,
            ordered_scale_steps=self.ordered_scale_steps,
        )
