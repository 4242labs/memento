"""Human-facing rendering, and nothing else (B-02 T8 R1).

Every message a person reads on the console is here. Every decision about *what happened* is in
`commands.py`. The split is the contract boundary:

> **Console prose is not contractual.** It is free to be reworded, reordered, or translated. A
> consumer that parses it is relying on something this project does not promise. The promises are
> the exit code and the `--json` payload, both of which live in the command layer.

That is also the mutation-ratchet boundary. A mutant that changes a message here changes nothing a
consumer can depend on, so this module is out of ratchet scope **by architecture** — not by an
exclusion list somebody has to keep in sync. Nothing here decides an exit code, and nothing here
touches the store; if a change to this file could alter either, it belongs in `commands.py`.

Renderers return `(stdout_text, stderr_text)`. They never print, so they stay testable and the
process's output happens in exactly one place.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .commands import Outcome

Rendered = tuple[str, str]


def _lines(*parts: str) -> str:
    body = "\n".join(p for p in parts if p)
    return body + "\n" if body else ""


def _field(label: str, value: Any) -> str:
    return f"{label:<16}{value}"


# ------------------------------------------------------------------- renderers


def _error(data: dict[str, Any]) -> Rendered:
    flags = [f"FLAG: {flag}" for flag in data.get("flags", [])]
    return "", _lines(*flags, str(data.get("error", "")))


def _status(data: dict[str, Any]) -> Rendered:
    out = [
        _field("store:", data["store"]),
        _field("schema:", data["schema"]),
        _field("documents:", ", ".join(data["documents"]) or "(none)"),
        _field("streams:", ", ".join(data["streams"]) or "(none)"),
        _field("session logs:", data["session_logs"]),
        _field("tombstones:", data["tombstones"]),
        _field(
            "backup:",
            "enabled -> " + str(data["backup"]["remote"]) if data["backup"]["enabled"] else "off",
        ),
    ]
    for claim in data["claims"]:
        state = "stale, reclaimable" if claim["stale"] else f"pid {claim['pid']}"
        out.append(_field("claim:", f"{claim['session']} ({state})"))
    if "pending" in data:
        out.append(_field("pending:", data["pending"]))
        if data.get("backlog"):
            out.append(_field("FLAG:", data["backlog"]))
    return _lines(*out), ""


def _document_list(data: dict[str, Any]) -> Rendered:
    return _lines(*data["documents"]), ""


def _document(data: dict[str, Any]) -> Rendered:
    return data["content"], ""  # verbatim: a document is content, not a report about content


def _facts(data: dict[str, Any]) -> Rendered:
    return json.dumps(data["facts"], indent=2, sort_keys=True) + "\n", ""


def _fingerprint(data: dict[str, Any]) -> Rendered:
    return data["fingerprint"] + "\n", ""


def _prefix(data: dict[str, Any]) -> Rendered:
    err = _lines(*(f"FLAG: {flag}" for flag in data["flags"]))
    return (data["text"] + "\n" if data["text"] else ""), err


def _history(data: dict[str, Any]) -> Rendered:
    if not data["revisions"]:
        return _lines(f"no recorded revisions for {data['document']}"), ""
    out = []
    for rev in data["revisions"]:
        marker = "" if rev["has_prior"] else "  (no prior content — rollback unavailable)"
        if rev["abandoned"]:
            marker += "  (abandoned — recorded but never written)"
        out.append(
            f"[{rev['ordinal']}] {rev['ts']}  session={rev['session']}  batch={rev['batch']}{marker}"
        )
    return _lines(*out), ""


def _recall(data: dict[str, Any]) -> Rendered:
    err = _lines(*(f"FLAG: {flag}" for flag in data["flags"]))
    if not data["hits"]:
        return _lines("no matches"), err
    out = []
    for hit in data["hits"]:
        where = f"{hit['location']}:{hit['entry_id']}" if hit["entry_id"] else hit["location"]
        out.append(f"[{hit['score']}] {where}  {hit['text']}")
    return _lines(*out), err


def _prompts(data: dict[str, Any]) -> Rendered:
    return data["text"], ""


def _rejected(data: dict[str, Any]) -> Rendered:
    out = ["REJECTED — nothing was written.", f"  {data.get('error', '')}"]
    out += [f"  {v}" for v in data.get("violations", [])]
    return "", _lines(*out)


def _consolidated(data: dict[str, Any]) -> Rendered:
    out = [f"wrote documents: {', '.join(data['documents'])}"]
    if data["streams"]:
        out.append(f"wrote streams:   {', '.join(data['streams'])}")
    return _lines(*out), ""


def _rolled_back(data: dict[str, Any]) -> Rendered:
    return _lines(f"rolled {data['document']} back to revision {data['revision']}"), ""


def _edited(data: dict[str, Any]) -> Rendered:
    if not data["changed"]:
        return _lines("unchanged"), ""
    return _lines(f"{data['document']} updated; prior content kept in the document_replaced history"), ""


def _forgotten(data: dict[str, Any]) -> Rendered:
    if not data["rerendered"]:
        return _lines(
            f"tombstoned {data['marker']}; it will be honored by every future fold and consolidation",
            "(no adapter given, so the projected documents were not re-rendered)",
        ), ""
    return _lines(f"forgot {data['marker']} and re-rendered the projected documents"), ""


def _backup_refused(data: dict[str, Any]) -> Rendered:
    return "", _lines(f"Refusing to enable backup without --yes.\n\n{data['warning']}")


def _backup_enabled(data: dict[str, Any]) -> Rendered:
    return _lines(f"backup enabled for {data['store']} -> {data['remote'] or '(local git only)'}"), ""


def _journal(data: dict[str, Any]) -> Rendered:
    return _lines(*(json.dumps(t, ensure_ascii=False, sort_keys=True) for t in data["turns"])), ""


def _journalled(data: dict[str, Any]) -> Rendered:
    return "", ""


def _enqueued(data: dict[str, Any]) -> Rendered:
    return _lines(
        f"enqueued {data['session']}; consolidate it later, after `pending --gate-check` passes"
    ), ""


def _pending(data: dict[str, Any]) -> Rendered:
    out = []
    for item in data["pending"]:
        deferred = f"  deferrals={item['deferrals']}" if item["deferrals"] else ""
        out.append(f"{item['session']}  enqueued_at={item['enqueued_at']:.0f}{deferred}")
    if not data["pending"]:
        out.append("nothing pending")
    # Deferred must never mean forgotten (ADR D3.5). This is that surface, for a consumer whose
    # only view of the queue is this command.
    err = _lines(f"FLAG: {data['backlog']['message']}") if data["backlog"]["message"] else ""
    return _lines(*out), err


def _gate_refused(data: dict[str, Any]) -> Rendered:
    return "", _lines(f"REFUSED — {data['error']}")


def _done(data: dict[str, Any]) -> Rendered:
    return _lines(f"marked {data['session']} consolidated"), ""


def _claimed(data: dict[str, Any]) -> Rendered:
    return data["token"] + "\n", ""


def _released(data: dict[str, Any]) -> Rendered:
    if data["released"]:
        return _lines(f"released {data['session']}"), ""
    return _lines(f"{data['session']} was not claimed"), ""


def _committed(data: dict[str, Any]) -> Rendered:
    if data.get("error"):
        return "", _lines(f"FLAG: {data['error']}")
    if not data["enabled"]:
        return _lines("backup is not enabled for this store; nothing to commit"), ""
    out = [f"committed {data['sha']}" if data["sha"] else "nothing to commit"]
    if data["pushed"]:
        out.append("pushed")
    return _lines(*out), ""


def _plain(data: dict[str, Any]) -> Rendered:
    return "", ""


RENDERERS: dict[str, Callable[[dict[str, Any]], Rendered]] = {
    "backup-enabled": _backup_enabled,
    "backup-refused": _backup_refused,
    "claimed": _claimed,
    "committed": _committed,
    "consolidated": _consolidated,
    "document": _document,
    "document-list": _document_list,
    "done": _done,
    "edited": _edited,
    "enqueued": _enqueued,
    "error": _error,
    "facts": _facts,
    "fingerprint": _fingerprint,
    "forgotten": _forgotten,
    "gate-refused": _gate_refused,
    "history": _history,
    "journal": _journal,
    "journalled": _journalled,
    "pending": _pending,
    "plain": _plain,
    "prefix": _prefix,
    "prompts": _prompts,
    "recall": _recall,
    "rejected": _rejected,
    "released": _released,
    "rolled-back": _rolled_back,
    "status": _status,
}


def render(outcome: Outcome) -> Rendered:
    """Human text for an outcome. An unknown kind reports the error it has rather than nothing."""
    renderer = RENDERERS.get(outcome.kind)
    if renderer is None:
        return _error(outcome.data)
    return renderer(outcome.data)
