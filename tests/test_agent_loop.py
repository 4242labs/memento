"""The agent consumer's whole write path, exercised the only way an agent can reach it (B-02 AC-1).

The engine's second consumer class has no Python: it is markdown and a shell. So nothing in this
module imports `memento` to do the work — every step goes out through `subprocess` and comes back as
an exit code, exactly as it does for `tortuga/agents/advisor-legal`. A test that reached into the
library would prove the library works and say nothing about the contract an agent actually has.

The claim tests are the sharp end. `SessionClaim` holds an `flock` for the acquiring process's
lifetime, which is right for the drain and useless here: an agent's consolidation spans several
`memento` invocations with the model's own thinking in between, so a claim that released when the
command exited would let a second front-end pay for the same consolidation. `test_a_process_scoped_
claim_does_not_exclude_across_processes` is that defect, demonstrated, and it is what the CAS claim
had to beat before it was written (handoff §5.1).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

SPEC = {
    "name": "agent-fixture",
    "prefix_budget_tokens": 400,
    "identity_keys": ["id", "topic", "name"],
    "documents": {
        "operator.md": {"title": "Operator", "sections": ["operator"]},
        "practice.md": {"title": "Practice", "sections": ["practice"]},
    },
    "prefix_sections": [
        {"name": "operator", "priority": 0, "document": "operator.md"},
        {"name": "practice", "priority": 1, "document": "practice.md"},
    ],
    "schema": {"operator.reply_style": {"type": "str", "enum": ["terse", "normal", "expansive"]}},
    "collections": {"practice": {"kind": "list", "identity_key": "topic"}},
    "distillation_prompt": "(fixture)",
}

FACTS = {
    "operator": {"reply_style": "terse", "timezone": "Europe/Lisbon"},
    "practice": [{"topic": "verify before asserting", "weight": "high"}],
}

SESSION = "260802-140000"


@pytest.fixture
def agent(tmp_path):
    """Paths and a runner. No `memento` import anywhere in the loop under test."""
    spec = tmp_path / "adapter.json"
    spec.write_text(json.dumps(SPEC), encoding="utf-8")

    def run(*argv: str, store: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "memento.cli", "--store", store or str(tmp_path / "memento"), *argv],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(_src())},
        )

    run.tmp = tmp_path  # type: ignore[attr-defined]
    run.spec = str(spec)  # type: ignore[attr-defined]
    run.store = str(tmp_path / "memento")  # type: ignore[attr-defined]
    run.queue = str(tmp_path / "queue")  # type: ignore[attr-defined]
    return run


def _src() -> str:
    import memento

    return os.path.dirname(os.path.dirname(os.path.abspath(memento.__file__)))


# ------------------------------------------------------------------ the whole loop


def test_an_agent_completes_the_full_loop_through_the_cli_alone(agent, tmp_path):
    """journal → enqueue → gate-check → claim → prefix → proposal → consolidate → commit → done.

    The order is the contract in `docs/agent-consumers.md`, and every step here is a process
    boundary. What it proves is not that each verb works alone — it is that they compose into a
    session lifecycle without a Python consumer holding them together.
    """
    # --- during the session
    assert agent("journal", SESSION, "--queue", agent.queue, "--text", "operator wants terse replies").returncode == 0
    assert agent("enqueue", SESSION, "--queue", agent.queue).returncode == 0

    # --- later: the gate decides whether consolidation may start at all
    too_soon = agent("pending", "--queue", agent.queue, "--gate-check", "--idle-seconds", "1")
    assert too_soon.returncode == 7, "an idle-seconds under the gate must refuse, not proceed"

    allowed = agent(
        "pending", "--queue", agent.queue, "--gate-check",
        "--idle-seconds", "30", "--prefix-materialized",
    )
    assert allowed.returncode == 0
    assert SESSION in allowed.stdout

    # --- claim, read, distill (the agent *is* the model), submit
    claimed = agent("claim", SESSION)
    assert claimed.returncode == 0
    token = claimed.stdout.strip()

    assert agent("prefix", "--adapter-file", agent.spec).returncode == 0
    journal = agent("journal", SESSION, "--queue", agent.queue, "--show")
    assert "terse replies" in journal.stdout

    proposal = tmp_path / "proposal.json"
    proposal.write_text(json.dumps({"facts": FACTS}), encoding="utf-8")
    wrote = agent(
        "consolidate", "--adapter-file", agent.spec, "--proposal", str(proposal),
        "--session", SESSION, "--queue", agent.queue, "--unchecked",
    )
    assert wrote.returncode == 0, wrote.stderr

    # --- the backup surface, then the marker, LAST
    assert agent("backup", "--yes").returncode == 0
    committed = agent("commit", "--session", SESSION, "--no-push")
    assert committed.returncode == 0, committed.stderr
    assert "committed" in committed.stdout

    assert agent("done", SESSION, "--queue", agent.queue).returncode == 0
    assert agent("release", SESSION, "--token", token).returncode == 0

    # --- and the session is gone from the backlog, because the marker says so
    after = agent("pending", "--queue", agent.queue)
    assert "nothing pending" in after.stdout

    prefix = agent("prefix", "--adapter-file", agent.spec)
    assert "Europe/Lisbon" in prefix.stdout


def test_the_compare_and_swap_survives_the_shell_and_the_second_writer_is_refused(agent, tmp_path):
    """Two agents, one store. The one whose baseline moved is told to redrive, not silently dropped."""
    first = tmp_path / "p1.json"
    first.write_text(json.dumps({"facts": FACTS}), encoding="utf-8")
    assert agent("consolidate", "--adapter-file", agent.spec, "--proposal", str(first),
                 "--session", "260802-140001", "--unchecked").returncode == 0

    stale = agent("facts", "--fingerprint", "--adapter-file", agent.spec).stdout.strip()

    grown = json.loads(json.dumps(FACTS))
    grown["practice"].append({"topic": "own the mistake first", "weight": "high"})
    second = tmp_path / "p2.json"
    second.write_text(json.dumps({"facts": grown}), encoding="utf-8")
    assert agent("consolidate", "--adapter-file", agent.spec, "--proposal", str(second),
                 "--session", "260802-140002", "--expect", stale).returncode == 0

    # A third proposal that the gates are perfectly happy with — it only grows — but which was
    # derived from the baseline the second writer has since replaced. Refused on the fingerprint
    # alone, which is the distinction worth having: rejected for being *late*, not for being wrong.
    late_facts = json.loads(json.dumps(grown))
    late_facts["practice"].append({"topic": "never present a menu", "weight": "high"})
    third = tmp_path / "p3.json"
    third.write_text(json.dumps({"facts": late_facts}), encoding="utf-8")
    late = agent("consolidate", "--adapter-file", agent.spec, "--proposal", str(third),
                 "--session", "260802-140003", "--expect", stale)
    assert late.returncode == 5
    assert "redrive" in late.stderr


def test_a_rejected_consolidation_defers_the_session_instead_of_losing_it(agent, tmp_path):
    """Exit 3 is not the end of the story — the queue has to remember the session is still owed."""
    good = tmp_path / "p1.json"
    good.write_text(json.dumps({"facts": FACTS}), encoding="utf-8")
    agent("journal", SESSION, "--queue", agent.queue, "--text", "x")
    agent("enqueue", SESSION, "--queue", agent.queue)
    agent("consolidate", "--adapter-file", agent.spec, "--proposal", str(good),
          "--session", SESSION, "--queue", agent.queue, "--unchecked")

    eroded = tmp_path / "p2.json"
    eroded.write_text(json.dumps({"facts": {"operator": FACTS["operator"], "practice": []}}), encoding="utf-8")
    rejected = agent("consolidate", "--adapter-file", agent.spec, "--proposal", str(eroded),
                     "--session", SESSION, "--queue", agent.queue, "--unchecked")
    assert rejected.returncode == 3
    assert "tombstone" in rejected.stderr

    listing = agent("pending", "--queue", agent.queue, "--json")
    payload = json.loads(listing.stdout)
    assert payload["pending"][0]["session"] == SESSION
    assert payload["pending"][0]["deferrals"] == 1, "a rejection must leave a deferral behind"


def test_done_refuses_a_session_that_was_never_enqueued(agent):
    assert agent("done", "260802-999999", "--queue", agent.queue).returncode == 1


# ------------------------------------------------------------------- the claim race


def _claim_in_a_subprocess(
    store: str, session: str, *, start_at: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Take the CAS claim in a *separate process*, then let that process exit.

    `start_at` is a wall-clock barrier, and it is the difference between a race and a queue.
    Spawning six interpreters takes far longer than the read-modify-write they contend over, so
    without it they arrive one at a time and the test passes against a claim with no mutual
    exclusion at all — the exact shape of the three regression tests that once passed against the
    code they were written to guard (handoff §5.1).
    """
    # Sleep to the barrier, then spin only for the last few milliseconds. A spin across the whole
    # wait pegs a core per process, and six of those under a mutation run — which is itself already
    # eight workers wide — starves the machine badly enough that the sweep appears to hang.
    barrier = (
        "import time\n"
        f"time.sleep(max(0.0, {start_at!r} - time.time() - 0.01))\n"
        f"while time.time() < {start_at!r}:\n"
        "    pass\n"
        if start_at is not None
        else ""
    )
    code = (
        "import sys\n"
        "from memento.store import MemoryStore\n"
        "from memento.locking import CasClaim\n"
        "from memento.errors import ClaimHeld\n"
        f"store = MemoryStore({store!r})\n"
        f"claim = CasClaim(store.locks_dir, {session!r})\n"
        + barrier
        + "try:\n"
        "    record = claim.acquire()\n"
        "    print(record.token)\n"
        "except ClaimHeld:\n"
        "    print('HELD', file=sys.stderr); sys.exit(6)\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": _src()},
    )


def _flock_claim_in_a_subprocess(store: str, session: str) -> subprocess.CompletedProcess[str]:
    """The same, on the process-scoped `SessionClaim`. This is the probe that must fail."""
    code = (
        "import sys;"
        "from memento.store import MemoryStore;"
        "from memento.locking import SessionClaim;"
        "from memento.errors import ClaimHeld;"
        f"store = MemoryStore({store!r});"
        f"claim = SessionClaim(store.locks_dir, {session!r});"
        "\ntry:\n"
        "    claim.acquire()\n"
        "    print('CLAIMED')\n"
        "except ClaimHeld:\n"
        "    print('HELD', file=sys.stderr); sys.exit(6)\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": _src()},
    )


def test_a_process_scoped_claim_does_not_exclude_across_processes(agent):
    """The defect the CAS claim exists to fix, kept as a live demonstration (B-02 AC-1b).

    `SessionClaim`'s `flock` dies with the process that took it. So a `claim` verb built on it hands
    the claim straight back on exit, and the *next* process claims the same session happily — which
    is not exclusion, it is bookkeeping. Two agents would then both pay for one consolidation.

    This asserts the broken behaviour on purpose. If it ever starts failing, `SessionClaim` has
    changed and the reason `CasClaim` exists needs re-reading, not the assertion adjusting.
    """
    agent("status")  # materialize the store so locks_dir exists
    first = _flock_claim_in_a_subprocess(agent.store, SESSION)
    second = _flock_claim_in_a_subprocess(agent.store, SESSION)

    assert first.returncode == 0
    assert second.returncode == 0, "an flock claim taken by a dead process excludes nobody"


def test_the_cas_claim_excludes_across_processes(agent):
    """The fix, against the same probe: the claim outlives the process that took it."""
    agent("status")
    first = _claim_in_a_subprocess(agent.store, SESSION)
    second = _claim_in_a_subprocess(agent.store, SESSION)

    assert first.returncode == 0
    assert second.returncode == 6, "the second process must be refused, not handed the same session"
    assert "HELD" in second.stderr


def test_two_concurrent_processes_contend_and_exactly_one_wins(agent):
    """The race, run as a race rather than in sequence — six processes, one claim, one instant."""
    agent("status")
    start_at = time.time() + 1.2  # every interpreter is up and waiting before any of them tries
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(lambda _: _claim_in_a_subprocess(agent.store, SESSION, start_at=start_at), range(6))
        )

    # Every outcome must be one of the two the contract names. An unguarded read-modify-write does
    # not merely double-claim — it tears the claim file and the losers die on the way in, which
    # would otherwise read as "only one winner" and pass.
    assert all(r.returncode in (0, 6) for r in results), [r.stderr for r in results]
    winners = [r for r in results if r.returncode == 0]
    losers = [r for r in results if r.returncode == 6]
    assert len(winners) == 1, f"{len(winners)} processes claimed the same session"
    assert len(losers) == 5
    assert all("HELD" in r.stderr for r in losers)


def test_the_claim_verb_refuses_a_release_under_the_wrong_token(agent):
    agent("status")
    token = agent("claim", SESSION).stdout.strip()
    assert agent("release", SESSION, "--token", "not-the-token").returncode == 6
    assert agent("release", SESSION, "--token", token).returncode == 0


def test_a_stale_claim_is_reclaimable_without_an_operator(agent):
    """Every claim expires. An agent that walked away must not wedge the session for good."""
    agent("status")
    assert agent("claim", SESSION, "--ttl", "0").returncode == 0
    again = agent("claim", SESSION)
    assert again.returncode == 0, "a claim past its TTL must be reclaimable by the next comer"


def test_done_honors_a_declared_retention_policy(agent, tmp_path):
    """Retention is the consumer's declaration, and `done` is the only verb that can act on it.

    Marking a session consolidated is what makes its transcript material eligible for pruning. A
    queue built without the adapter keeps everything — the safe default, and the wrong answer for a
    consumer that stated otherwise out loud.
    """
    pruning = tmp_path / "pruning.json"
    pruning.write_text(
        json.dumps({**SPEC, "retention": {"keep_everything": False, "prune_after_consolidation": True}}),
        encoding="utf-8",
    )
    agent("journal", SESSION, "--queue", agent.queue, "--text", "material")
    agent("enqueue", SESSION, "--queue", agent.queue)
    journal = tmp_path / "queue" / SESSION / "journal.jsonl"
    assert journal.exists()

    assert agent("done", SESSION, "--queue", agent.queue, "--adapter-file", str(pruning)).returncode == 0
    assert not journal.exists(), "a declared prune policy was ignored"


def test_done_keeps_everything_when_no_adapter_says_otherwise(agent, tmp_path):
    """The control: the default is to keep, and it stays the default."""
    agent("journal", SESSION, "--queue", agent.queue, "--text", "material")
    agent("enqueue", SESSION, "--queue", agent.queue)
    journal = tmp_path / "queue" / SESSION / "journal.jsonl"

    assert agent("done", SESSION, "--queue", agent.queue).returncode == 0
    assert journal.exists()
