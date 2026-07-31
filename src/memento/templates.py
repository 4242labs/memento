"""Engine-owned prompt templates, SHA-pinned (ADR D1).

The engine ships **relationship/restraint** templates; the consumer owns **domain distillation**
prompts. The split matters: restraint is a property of the engine's write discipline, so it cannot
be something each adapter quietly rewrites.

Pinning is enforced, not documented. `templates.lock.json` records the sha256 of each template and
`load` refuses a mismatch — a consumer pins the engine by git SHA, and this makes the prompt text
part of that pin rather than something that can drift underneath it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .errors import MementoError

TEMPLATE_DIR = Path(__file__).parent / "prompts"
LOCK_FILE = TEMPLATE_DIR / "templates.lock.json"

RELATIONSHIP = "relationship"
RESTRAINT = "restraint"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def template_path(name: str) -> Path:
    return TEMPLATE_DIR / f"{name}.md"


def read_lock() -> dict[str, str]:
    if not LOCK_FILE.exists():
        raise MementoError("templates.lock.json is missing; the engine build is incomplete")
    return json.loads(LOCK_FILE.read_text(encoding="utf-8"))


def load(name: str) -> str:
    """Load a pinned template, verifying its hash."""
    path = template_path(name)
    if not path.exists():
        raise MementoError(f"no such template: {name}")
    text = path.read_text(encoding="utf-8")
    expected = read_lock().get(name)
    if expected is None:
        raise MementoError(f"template {name!r} is not pinned in templates.lock.json")
    actual = _sha256(text)
    if actual != expected:
        raise MementoError(
            f"template {name!r} does not match its pin ({actual[:12]} != {expected[:12]}); "
            "re-pin deliberately with `python -m memento.templates --repin`"
        )
    return text


def all_names() -> list[str]:
    return sorted(p.stem for p in TEMPLATE_DIR.glob("*.md"))


def compute_lock() -> dict[str, str]:
    return {name: _sha256(template_path(name).read_text(encoding="utf-8")) for name in all_names()}


def repin() -> dict[str, str]:
    """Rewrite the lock file. Deliberate, manual, and never called by the engine at runtime."""
    lock = compute_lock()
    LOCK_FILE.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return lock


def preamble() -> str:
    """Both engine templates, in the order they should appear in a system prompt."""
    return load(RELATIONSHIP) + "\n\n" + load(RESTRAINT)


def check() -> list[str]:
    """Names whose on-disk content no longer matches the lock. Empty means the pins hold."""
    on_disk, pinned = compute_lock(), read_lock()
    return sorted(n for n in set(on_disk) | set(pinned) if on_disk.get(n) != pinned.get(n))


if __name__ == "__main__":  # pragma: no cover - maintenance entrypoint
    import sys

    if "--repin" in sys.argv:
        print(json.dumps(repin(), indent=2))
    elif "--check" in sys.argv:
        drifted = check()
        if drifted:
            print(f"prompt templates changed without re-pinning: {', '.join(drifted)}")
            raise SystemExit(1)
        print("template pins hold")
    else:
        print(json.dumps(read_lock(), indent=2))
