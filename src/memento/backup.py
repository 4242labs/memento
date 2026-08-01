"""Optional git backup (ADR D8).

**Off by default and impossible to switch on by accident.** A store is plain files; it gains
`git init` and a remote only when its owner opts in explicitly, having been shown the warning. There
is no "enable on first push" path and no default remote.

Lock discipline (ADR D3.3):

* `add` + `commit` run **under** the store lock — local I/O, bounded.
* `pull` + `push` run **outside** it. Network I/O never runs under the lock, because a hung push
  must not stall the other front-end.
* Every git subprocess carries an explicit timeout.

Attribution (ADR D3.4): each drained consolidation commits immediately, carrying the **consolidated**
session's id — never batched under a later session's.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import BackupError
from .locking import StoreLock
from .store import MemoryStore

CONFIG_DOCUMENT = ".memento/backup.json"
DEFAULT_TIMEOUT = 30.0

OPT_IN_WARNING = (
    "Enabling backup copies this store to a remote you control. The store holds distilled personal "
    "memory about its operator. Use a PRIVATE remote, and understand that every future consolidation "
    "will be pushed there automatically from the drain subprocess."
)

STORE_GITIGNORE = """\
# The queue and the locks are the store's unversioned area. They must never be pushed.
.memento/queue/
.memento/locks/
"""


@dataclass(frozen=True)
class BackupConfig:
    enabled: bool = False
    remote: str | None = None
    branch: str = "main"
    timeout: float = DEFAULT_TIMEOUT


def _has_inline_credentials(remote: str) -> bool:
    """`https://user:token@host/...` — a password in a URL the store would then persist."""
    match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/@]+)@", remote)
    return bool(match and ":" in match.group(1))


def _ensure_gitignore(store: MemoryStore, queue_root: str | os.PathLike[str] | None = None) -> None:
    """Guarantee the unversioned area is ignored, extending a pre-existing file rather than skipping.

    A store that already has a `.gitignore` — the adopted-store case — would otherwise have its
    locks and queue committed and pushed by the drain's `git add -A`.
    """
    wanted = [line for line in STORE_GITIGNORE.splitlines() if line]
    if queue_root is not None:
        # The queue is unversioned wherever the consumer actually put it. Hard-coding one path meant
        # a queue at `<store>/sessions-data` — jubs' own name — was committed and pushed.
        queue = Path(queue_root).resolve()
        if store.root == queue or store.root in queue.parents:
            wanted.append(f"/{queue.relative_to(store.root).as_posix()}/")

    path = store.root / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    missing = [line for line in wanted if line not in existing.splitlines()]
    if not missing:
        return
    prefix = existing if existing.endswith("\n") or not existing else existing + "\n"
    path.write_text(prefix + "\n".join(missing) + "\n", encoding="utf-8")


def read_config(store: MemoryStore) -> BackupConfig:
    raw = store.read_document(CONFIG_DOCUMENT)
    if not raw:
        return BackupConfig()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackupError(f"unreadable backup config in {store.root}") from exc
    return BackupConfig(
        enabled=bool(data.get("enabled", False)),
        remote=data.get("remote"),
        branch=data.get("branch", "main"),
        timeout=float(data.get("timeout", DEFAULT_TIMEOUT)),
    )


def is_enabled(store: MemoryStore) -> bool:
    return read_config(store).enabled


def git(
    store: MemoryStore, *args: str, timeout: float = DEFAULT_TIMEOUT, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=store.root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise BackupError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise BackupError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def enable_backup(
    store: MemoryStore,
    *,
    acknowledged: bool,
    remote: str | None = None,
    branch: str = "main",
    timeout: float = DEFAULT_TIMEOUT,
    queue_root: str | os.PathLike[str] | None = None,
) -> BackupConfig:
    """Opt a store into git backup. Refuses unless the caller passes the acknowledgement.

    Pass `queue_root` when the queue lives inside the store, so it is excluded from what gets
    pushed — the queue is the unversioned half of the store contract and holds the verbatim pile.
    """
    if not acknowledged:
        raise BackupError(f"backup requires explicit opt-in. {OPT_IN_WARNING}")

    if remote and _has_inline_credentials(remote):
        # Checked first, so a refusal cannot leave the store half-configured with a git repo and an
        # origin already set. A token in the URL would also be written to the store in plain text,
        # which is the one thing D8 says must never happen.
        raise BackupError(
            "the remote URL carries an inline credential. Use SSH, or a git credential helper, so "
            "the secret never enters the store"
        )

    store.initialize()
    _ensure_gitignore(store, queue_root)

    if not (store.root / ".git").exists():
        git(store, "init", "-b", branch, timeout=timeout)
        git(store, "config", "user.name", "memento", timeout=timeout)
        git(store, "config", "user.email", "memento@42labs.local", timeout=timeout)

    if remote:
        existing = git(store, "remote", timeout=timeout).stdout.split()
        if "origin" in existing:
            git(store, "remote", "set-url", "origin", remote, timeout=timeout)
        else:
            git(store, "remote", "add", "origin", remote, timeout=timeout)

    config = BackupConfig(enabled=True, remote=remote, branch=branch, timeout=timeout)
    store.replace_document(
        CONFIG_DOCUMENT,
        json.dumps(
            {
                "enabled": True,
                "remote": remote,
                "branch": branch,
                "timeout": timeout,
                "warning_shown": OPT_IN_WARNING,
            },
            indent=2,
        )
        + "\n",
        session="backup-optin",
        batch="enable",
    )
    return config


def commit_consolidation(
    store: MemoryStore,
    session: str,
    *,
    lock: StoreLock | None = None,
) -> str | None:
    """Commit one consolidation, attributed to the session that was consolidated.

    Returns the commit SHA, or None on a git-less store — where there is nothing to do and the
    audit trail is the event log instead.
    """
    config = read_config(store)
    if not config.enabled:
        return None
    lock = lock or StoreLock.for_store(store.locks_dir)
    with lock.hold():
        versioned = store.versioned_paths()
        git(store, "add", "-A", "--", *versioned, timeout=config.timeout)
        # Scoped to the same pathspec the add used. A whole-repo status also reports files the
        # engine deliberately does not version, and those can never be staged — so the guard read
        # "there is something to commit", `git commit` found an empty index and exited non-zero,
        # and every backup from then on raised. One stray untracked file under the store root is
        # enough, and a store root is not the engine's alone: jubs' had two `.gitkeep`s.
        status = git(
            store, "status", "--porcelain", "--", *versioned, timeout=config.timeout
        ).stdout.strip()
        if not status:
            return None
        git(store, "commit", "-m", f"memory: consolidate session {session}", timeout=config.timeout)
        return git(store, "rev-parse", "HEAD", timeout=config.timeout).stdout.strip()


def push(store: MemoryStore) -> bool:
    """Sync with the remote. Deliberately not under the store lock — this is network I/O."""
    config = read_config(store)
    if not config.enabled or not config.remote:
        return False
    git(store, "pull", "--rebase", "origin", config.branch, timeout=config.timeout, check=False)
    git(store, "push", "origin", config.branch, timeout=config.timeout)
    return True


def commit_messages(store: MemoryStore) -> list[str]:
    """Commit subjects, newest first. Used by the harness to assert attribution."""
    if not (store.root / ".git").exists():
        return []
    proc = git(store, "log", "--format=%s", check=False)
    return [line for line in proc.stdout.splitlines() if line.strip()]


def backup_root_is_store(store: MemoryStore) -> bool:
    """True when the git repo backing this store is the store itself, not an enclosing repo.

    Worth checking before any commit: committing a store from an app repo's git is the exact
    pollution D1 exists to prevent.
    """
    return (Path(store.root) / ".git").exists()
