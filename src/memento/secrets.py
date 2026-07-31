"""Secrets never enter the store (ADR D8).

A write-path gate, not a scanner you run afterwards: content matching a secret pattern is rejected
before anything lands. Fail closed — a false positive costs one deferred consolidation, a false
negative puts a live credential in a plain file that a backup may push to a remote.
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
