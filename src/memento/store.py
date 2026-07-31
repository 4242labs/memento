"""The store (ADR D1/D2).

Plain human-readable files under one `store_root`: append-only JSONL event logs, projected markdown
documents, a free-prose session log directory, and one engine-owned `.memento/` area.

**The store IS the namespace.** There is no `(agent, user)` tuple and no multi-tenant seam — an
instance can only ever reach paths under its own root, enforced here rather than by convention.

Layout (byte-compatible with today's jubs store — adopting it requires no migration):

    <store_root>/
      profile.md              projected document
      interests.md            projected document
      errors/en.jsonl         event stream "errors/en"
      vocab/en.jsonl          event stream "vocab/en"
      sessions/log-*.md       free-prose session logs
      .memento/               engine area (added; nothing pre-existing moves)
        schema_version
        documents.jsonl       the document_replaced audit stream
        objects/<sha256>      retained content-addressed prior document contents
        locks/                store lock + per-session claims
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .clock import DEFAULT_CLOCK, Clock
from .errors import CorruptStoreError, MementoError, SchemaVersionError, SecretsDetected
from .events import EVENT_DOCUMENT_REPLACED, Event, EventLog
from .fold import FoldedEntry, fold
from .ids import validate_session_id
from .secrets import scan_many

SCHEMA_VERSION = "1"

ENGINE_DIR = ".memento"
DOCUMENT_STREAM = f"{ENGINE_DIR}/documents"
OBJECTS_DIR = f"{ENGINE_DIR}/objects"
LOCKS_DIR = f"{ENGINE_DIR}/locks"
SESSIONS_DIR = "sessions"

# Queue filenames, mirrored from `queue.py` so the store can recognise queue material by shape.
JOURNAL_NAME = "journal.jsonl"
CONSOLIDATED_NAME = "consolidated"

# Prior document contents above this size are kept in the content-addressed area instead of inline
# in the event. The area is retained, so rollback stays available either way (ADR D2 round-6 minor).
DEFAULT_INLINE_LIMIT = 64 * 1024


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DocumentWrite:
    name: str
    content: str


@dataclass(frozen=True)
class ReplaceResult:
    document: str
    event: Event
    replayed: bool


class MemoryStore:
    def __init__(
        self,
        store_root: str | os.PathLike[str],
        *,
        clock: Clock = DEFAULT_CLOCK,
        inline_limit: int = DEFAULT_INLINE_LIMIT,
    ) -> None:
        self.root = Path(store_root).resolve()
        self.clock = clock
        self.inline_limit = inline_limit
        self._check_schema_version()

    # ------------------------------------------------------------------ paths

    def _resolve(self, relative: str) -> Path:
        """Resolve a store-relative path, refusing anything that escapes the root.

        This is the namespace guarantee in one function: a store cannot be talked into reading or
        writing another store's files, whatever an adapter passes in.
        """
        if os.path.isabs(relative):
            raise MementoError(f"store paths are relative to the store root: {relative!r}")
        path = (self.root / relative).resolve()
        if path != self.root and self.root not in path.parents:
            raise MementoError(f"path escapes the store root: {relative!r}")
        return path

    def stream_path(self, stream: str) -> Path:
        return self._resolve(f"{stream}.jsonl")

    def document_path(self, name: str) -> Path:
        return self._resolve(_document_name(name))

    @property
    def engine_dir(self) -> Path:
        return self._resolve(ENGINE_DIR)

    @property
    def locks_dir(self) -> Path:
        return self._resolve(LOCKS_DIR)

    # ------------------------------------------------------------ schema mark

    def _schema_marker(self) -> Path:
        return self._resolve(f"{ENGINE_DIR}/schema_version")

    def _check_schema_version(self) -> None:
        marker = self._schema_marker()
        if not marker.exists():
            return  # a pre-engine store (jubs today) reads fine; the marker lands on first write
        found = marker.read_text(encoding="utf-8").strip()
        if found != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"store at {self.root} is schema {found!r}; this engine speaks {SCHEMA_VERSION!r}"
            )

    def initialize(self) -> None:
        """Create the engine area. Idempotent, and it never touches pre-existing files."""
        self.engine_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        marker = self._schema_marker()
        if not marker.exists():
            _atomic_write_text(marker, SCHEMA_VERSION + "\n")

    # ---------------------------------------------------------------- streams

    def log(self, stream: str) -> EventLog:
        return EventLog(self.stream_path(stream), clock=self.clock)

    def streams(self) -> list[str]:
        """Every event stream in the store, engine area included, as stream ids.

        A symlink pointing out of the store is skipped rather than listed: listing it would hand
        callers an id that every read then refuses, which reads as corruption instead of what it is.
        """
        out = []
        for path in sorted(self.root.rglob("*.jsonl")):
            resolved = path.resolve()
            if self.root not in resolved.parents:
                continue
            out.append(str(path.relative_to(self.root).with_suffix("")))
        return out

    def append(
        self,
        stream: str,
        entries: Iterable[dict[str, Any] | Event],
        *,
        session: str,
        batch: str,
        turn: int | None = None,
    ) -> list[Event]:
        entries = list(entries)
        _reject_secrets(
            (
                f"{stream}[{i}]",
                json.dumps(
                    e.to_obj() if isinstance(e, Event) else e, ensure_ascii=False, default=str
                ),
            )
            for i, e in enumerate(entries)
        )
        self.initialize()
        return self.log(stream).append_batch(entries, session=session, batch=batch, turn=turn)

    def folded(self, stream: str) -> dict[str, FoldedEntry]:
        return fold(self.log(stream).read())

    # -------------------------------------------------------------- documents

    def read_document(self, name: str) -> str | None:
        path = self.document_path(name)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def documents(self) -> list[str]:
        return sorted(p.name for p in self.root.glob("*.md") if p.name != "README.md")

    def document_log(self) -> EventLog:
        return self.log(DOCUMENT_STREAM)

    def document_history(self, name: str) -> list[Event]:
        name = _document_name(name)
        return [
            e
            for e in self.document_log().read()
            if e.event == EVENT_DOCUMENT_REPLACED and e.payload.get("document") == name
        ]

    def replace_documents(
        self,
        writes: Sequence[DocumentWrite],
        *,
        session: str,
        batch: str,
        turn: int | None = None,
        scan_secrets: bool = True,
    ) -> list[ReplaceResult]:
        """Atomically replace one or more projected documents, wholesale.

        Ordering is event-first, file-second, and it has to be: an event appended before the replace
        still carries the true prior content, so a crash between the two loses nothing. Replaying
        the identical write completes the swap *without* appending a second event — the
        `(session, batch, document, ordinal)` idempotency key doing its job. The reverse order (file
        first) would lose the prior content irrecoverably on a git-less store.

        The recorded batch id also carries a digest of what is being written. A redrive after a
        crash re-runs the model, whose output is not byte-identical, and that is the ordinary case
        rather than an error: a different digest is simply a different write, which records the
        *current* file as its prior and lands normally. Treating it as a conflict instead is how a
        session becomes permanently unconsolidatable.
        """
        if not writes:
            return []
        names = [_document_name(w.name) for w in writes]
        if len(set(names)) != len(names):
            raise MementoError("a single batch may not replace the same document twice")

        if scan_secrets:
            _reject_secrets((f"document:{n}", w.content) for n, w in zip(names, writes))
        self.initialize()
        log = self.document_log()
        write_batch = _write_batch_id(batch, names, [w.content for w in writes])
        prior_batch = [e for e in log.read() if e.batch_key == (session, write_batch)]

        if prior_batch:
            return self._replay_replace(prior_batch, names, writes)

        history = [e for e in log.read() if e.event == EVENT_DOCUMENT_REPLACED]
        payloads = []
        for name, write in zip(names, writes):
            path = self.document_path(name)
            prior = path.read_text(encoding="utf-8") if path.exists() else None
            prior_sha = sha256_text(prior) if prior is not None else None
            payload: dict[str, Any] = {
                "event": EVENT_DOCUMENT_REPLACED,
                "document": name,
                "new_sha256": sha256_text(write.content),
                "prior_sha256": prior_sha,
            }
            previous = next(
                (e for e in reversed(history) if e.payload.get("document") == name), None
            )
            if previous is not None and previous.payload.get("new_sha256") != prior_sha:
                # The previous revision was recorded and then abandoned — a crash between the event
                # and the file swap, followed by a redrive with different output. Say so, rather
                # than leaving a chain whose links quietly do not meet: the log is the audit history,
                # and a gap it does not admit to is worse than one it does.
                payload["supersedes_abandoned"] = previous.batch
            if prior is None:
                payload["prior_content"] = None
            elif len(prior.encode("utf-8")) <= self.inline_limit:
                payload["prior_content"] = prior
            else:
                payload["prior_ref"] = self._put_object(prior)
            payloads.append(payload)

        events = log.append_batch(payloads, session=session, batch=write_batch, turn=turn)
        for name, write in zip(names, writes):
            _atomic_write_text(self.document_path(name), write.content)
        return [
            ReplaceResult(document=name, event=ev, replayed=False)
            for name, ev in zip(names, events)
        ]

    def replace_document(
        self,
        name: str,
        content: str,
        *,
        session: str,
        batch: str,
        turn: int | None = None,
        scan_secrets: bool = True,
    ) -> ReplaceResult:
        """Single-document convenience.

        One `(session, batch)` covers one call. Replacing several documents in the same batch means
        one `replace_documents` call with all of them — a second call under the same key is a replay,
        not a second write.
        """
        return self.replace_documents(
            [DocumentWrite(name, content)],
            session=session,
            batch=batch,
            turn=turn,
            scan_secrets=scan_secrets,
        )[0]

    def _replay_replace(
        self,
        prior_batch: list[Event],
        names: list[str],
        writes: Sequence[DocumentWrite],
    ) -> list[ReplaceResult]:
        by_key = {(e.payload.get("document"), e.ordinal): e for e in prior_batch}
        results = []
        for ordinal, (name, write) in enumerate(zip(names, writes)):
            ev = by_key.get((name, ordinal))
            if ev is None:  # pragma: no cover - the digest in the batch id rules this out
                raise CorruptStoreError(
                    f"recorded batch is missing document {name!r} at ordinal {ordinal}"
                )
            path = self.document_path(name)
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != write.content:
                _atomic_write_text(path, write.content)  # finish the interrupted replace
            results.append(ReplaceResult(document=name, event=ev, replayed=True))
        return results

    def prior_content(self, event: Event) -> str | None:
        """The content a `document_replaced` event superseded, inline or from the object area."""
        if "prior_content" in event.payload:
            return event.payload["prior_content"]
        ref = event.payload.get("prior_ref")
        if ref is None:
            return None
        path = self._resolve(f"{OBJECTS_DIR}/{ref}")
        if not path.exists():
            raise CorruptStoreError(
                f"prior content {ref} is missing from the retained object area; "
                "rollback is unavailable for this revision"
            )
        return path.read_text(encoding="utf-8")

    def _put_object(self, content: str) -> str:
        digest = sha256_text(content)
        path = self._resolve(f"{OBJECTS_DIR}/{digest}")
        if not path.exists():
            _atomic_write_text(path, content)
        return digest

    # --------------------------------------------------------- session logs

    def versioned_paths(self) -> list[str]:
        """The parts of the store git may track, as store-relative pathspecs.

        Explicit rather than `add -A`, because the store root is not the engine's alone: a consumer
        may put its queue — the unbounded verbatim pile — anywhere beneath it, and a blanket add
        pushes that to the remote. Only what the engine itself versions is offered to git.
        """
        keep = [
            f"{ENGINE_DIR}/schema_version",
            f"{ENGINE_DIR}/facts.json",
            f"{ENGINE_DIR}/documents.jsonl",
            f"{ENGINE_DIR}/tombstones.jsonl",
            f"{ENGINE_DIR}/backup.json",
            OBJECTS_DIR,
            SESSIONS_DIR,
            ".gitignore",
        ]
        keep += sorted(p.name for p in self.root.glob("*.md"))
        keep += [
            f"{stream}.jsonl"
            for stream in self.streams()
            if not stream.startswith(f"{ENGINE_DIR}/") and not self._is_queue_material(stream)
        ]
        return [p for p in dict.fromkeys(keep) if (self.root / p).exists()]

    def _is_queue_material(self, stream: str) -> bool:
        """True for a journal in the queue area, wherever the consumer put it.

        The queue's filenames are the engine's own (`journal.jsonl`, the `consolidated` marker), so
        it can be recognised by shape rather than by trusting a declaration. A consumer that puts
        its queue at `<store>/sessions-data` — a plausible layout — otherwise had its verbatim
        transcript pile committed and pushed by the backup.
        """
        path = self.root / f"{stream}.jsonl"
        if path.name == JOURNAL_NAME:
            return True
        return (path.parent / CONSOLIDATED_NAME).exists()

    def write_session_log(self, session: str, text: str) -> Path:
        validate_session_id(session)
        _reject_secrets([(f"session-log:{session}", text)])
        path = self._resolve(f"{SESSIONS_DIR}/log-{session}.md")
        _atomic_write_text(path, text)
        return path

    def read_session_log(self, session: str) -> str | None:
        validate_session_id(session)
        path = self._resolve(f"{SESSIONS_DIR}/log-{session}.md")
        return path.read_text(encoding="utf-8") if path.exists() else None

    def session_logs(self) -> list[str]:
        directory = self._resolve(SESSIONS_DIR)
        if not directory.exists():
            return []
        return sorted(p.name for p in directory.glob("log-*.md"))


def _reject_secrets(items: Iterable[tuple[str, str]] | Iterator[tuple[str, str]]) -> None:
    """The secrets gate, at the last door before disk.

    `apply_consolidation` scans earlier so a whole consolidation is rejected before anything is
    written. This is the backstop for every other way in — an operator `edit`, a rollback, a direct
    `replace_document` — because a gate that only guards the main path is not a gate.
    """
    found = scan_many(items)
    if found:
        raise SecretsDetected("; ".join(m.render() for m in found))


def _write_batch_id(batch: str, names: Sequence[str], contents: Sequence[str]) -> str:
    """`batch` plus a digest of exactly what is being written.

    Same batch, same bytes → same id → a replay. Same batch, different bytes → a different id → a
    new revision rather than a conflict.
    """
    digest = hashlib.sha256()
    for name, content in zip(names, contents):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_text(content).encode("ascii"))
        digest.update(b"\0")
    return f"{batch}#{digest.hexdigest()[:12]}"


def _document_name(name: str) -> str:
    """Documents default to markdown; anything with an explicit suffix is left alone."""
    return name if Path(name).suffix else f"{name}.md"


def _atomic_write_text(path: Path, content: str) -> None:
    """Wholesale replace via a same-directory temp file and `os.replace`.

    A reader either sees the old file or the new one, never a half-written one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
