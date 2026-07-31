"""Session-id validation.

The store resolves every path against its root and refuses anything that escapes. The queue and the
lock directory need the same guarantee, and they build paths straight from a session id — so the id
itself has to be a single, ordinary path segment before it is ever joined to a root.

Without this, a session id of `../../x` writes outside the queue and, with pruning enabled, deletes
outside it too.
"""

from __future__ import annotations

import re

from .errors import MementoError

_VALID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_session_id(session: str) -> str:
    """Return `session` if it is a safe single path segment; raise otherwise."""
    if not isinstance(session, str) or not _VALID.match(session) or ".." in session:
        raise MementoError(
            f"invalid session id {session!r}: expected a single path segment of letters, digits, "
            "dot, dash or underscore, starting with a letter or digit"
        )
    return session
