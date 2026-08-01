"""The one place that knows what a fake credential looks like.

Every credential-shaped string in this repo comes from here, and every one is **fabricated**. They
exist so the write-path secrets gate has something to reject in a test.

They are assembled from fragments rather than written as literals. A literal would be committed,
and a secret in git history is not removable without rewriting it — which is why the fix for one is
always a rewrite or an exception, and neither is free. Keeping them out of the source text keeps
that choice off the table entirely.

`AKIAIOSFODNN7EXAMPLE` is AWS's own published example key. It is not, and never was, live.
"""

from __future__ import annotations

_SAMPLES: dict[str, tuple[str, ...]] = {
    "aws-access-key": ("AKIA", "IOSFODNN7EXAMPLE"),
    "github-token": ("ghp_", "a" * 36),
    "anthropic-key": ("sk-ant-", "b" * 40),
    "openai-key": ("sk-", "c" * 40),
    "private-key-block": ("-----BEGIN RSA PRIVATE ", "KEY-----"),
    "assigned-secret": ("api_key", ' = "0123456789abcdefghij"'),
    "url-with-credential": ("https://x-access-token:ghp_", "d" * 36, "@github.com/owner/repo.git"),
}


def fake_credential(kind: str) -> str:
    """A fabricated credential of the given kind, assembled at runtime."""
    try:
        return "".join(_SAMPLES[kind])
    except KeyError:
        raise KeyError(f"no fake credential of kind {kind!r}; known: {sorted(_SAMPLES)}") from None


def kinds() -> list[str]:
    return sorted(_SAMPLES)
