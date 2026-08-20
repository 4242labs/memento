"""Secrets never enter the store (ADR D8).

A write-path gate, not a scanner you run afterwards: content matching a secret pattern is rejected
before anything lands. Fail closed — a false positive costs one deferred consolidation, a false
negative puts a live credential in a plain file that a backup may push to a remote.

The same door refuses hostile Unicode. Store content is replayed into future prompts, so text that
reads differently to a human auditor than to the model — bidi reordering, tag-block smuggling,
zero-width padding — is a trust-boundary breach, not a formatting quirk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

_PATTERNS: tuple[tuple[str, str], ...] = (
    ("private-key-block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("aws-access-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ("slack-token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    ("anthropic-key", r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    ("openai-key", r"\bsk-[A-Za-z0-9]{32,}\b"),
    ("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("bearer-token", r"\bBearer\s+[A-Za-z0-9._-]{24,}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ("assigned-secret", r"(?i)\b(api[_-]?key|secret|passwd|password|token)\b\s*[:=]\s*['\"]?[A-Za-z0-9/+_-]{16,}"),
    # Hostile Unicode. Characters with a real orthographic or tooling use stay admissible, because
    # a *systematic* false positive makes a store permanently unconsolidatable for the users whose
    # language needs them: ZWJ (emoji sequences), ZWNJ (Persian), LRM/RLM and the bidi isolates
    # U+2066–U+2069 (RTL templating), soft hyphen (copied web text), and U+FEFF at offset 0 (an
    # editor's BOM). The reordering embeds/overrides, the tag block, zero-width padding, and the
    # blank-rendering Hangul fillers are refused.
    ("bidi-control", r"[\u202A-\u202E]"),
    ("unicode-tag", r"[\U000E0000-\U000E007F]"),
    ("invisible-unicode", r"[\u200B\u2060-\u2064\u180E\u115F\u1160\u3164\uFFA0]|(?<=[\s\S])\uFEFF"),
)

COMPILED = tuple((name, re.compile(pattern)) for name, pattern in _PATTERNS)


@dataclass(frozen=True)
class SecretMatch:
    pattern: str
    where: str

    def render(self) -> str:
        return f"{self.pattern} in {self.where}"


def scan(text: str, *, where: str = "<text>") -> list[SecretMatch]:
    if not text:
        return []
    return [SecretMatch(name, where) for name, rx in COMPILED if rx.search(text)]


def scan_many(items: Iterable[tuple[str, str]]) -> list[SecretMatch]:
    """Scan (where, text) pairs, returning every match across all of them."""
    out: list[SecretMatch] = []
    for where, text in items:
        out.extend(scan(text, where=where))
    return out
