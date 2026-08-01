"""The live tier (ADR D6) — **non-gating**, tolerance-based, run on demand.

CI never runs this. It needs a real model, real credentials, and real token spend, and a flaky model
day must not turn the build red — the deterministic tier is what gates, precisely because it can
answer "is the write discipline correct?" without asking a model anything.

What this tier is for: catching the failures a stub cannot see — a distillation prompt that has
drifted out of alignment with the gates, a model that has started proposing two-step jumps, a
consolidation that technically passes but says nothing useful.

Run it with a consumer's real distiller:

    MEMENTO_LIVE_DISTILLER=myapp.memory:DISTILLER \\
    MEMENTO_LIVE_ADAPTER=myapp.memory:ADAPTER \\
    pytest tests/live -m live
"""

from __future__ import annotations

import importlib
import os

import pytest

from memento.writepath import UNCHECKED
from memento import GateFailure, MemoryStore, Queue, StoreState, apply_consolidation

pytestmark = pytest.mark.live

ADAPTER_REF = os.environ.get("MEMENTO_LIVE_ADAPTER")
DISTILLER_REF = os.environ.get("MEMENTO_LIVE_DISTILLER")

# How much rejection is tolerable before the prompt, not the model, is the suspect.
MAX_REJECTION_RATE = 0.25
ROUNDS = int(os.environ.get("MEMENTO_LIVE_ROUNDS", "4"))

SYNTHETIC_JOURNAL = [
    {"turn": 1, "said": "I have been learning Italian for about three months."},
    {"turn": 2, "said": "I goed to the market yesterday and buyed some bread."},
    {"turn": 3, "said": "I am not interested in sports, but I love cooking."},
]


def _resolve(ref: str):
    module_name, _, attr = ref.partition(":")
    obj = getattr(importlib.import_module(module_name), attr)
    return obj() if isinstance(obj, type) else obj


requires_live = pytest.mark.skipif(
    not (ADAPTER_REF and DISTILLER_REF),
    reason="set MEMENTO_LIVE_ADAPTER and MEMENTO_LIVE_DISTILLER to run the live tier",
)


@requires_live
def test_a_real_distillation_passes_the_gates_most_of_the_time(tmp_path):
    """Tolerance, not certainty: an occasional rejection is the gates working, not a failure."""
    adapter = _resolve(ADAPTER_REF)
    distiller = _resolve(DISTILLER_REF)
    rejected = 0

    for i in range(ROUNDS):
        store = MemoryStore(tmp_path / f"round-{i}")
        try:
            # `distill` is inside the try, not before it. A consumer's distiller runs its own
            # domain gates before it can even build a Proposal — jubs re-derives entry ids and
            # folds a type alias the model reaches for — and a rejection there is exactly the
            # prompt/gate disagreement this tier measures. Left outside, the FIRST such round
            # aborted the whole run with an exception instead of counting toward tolerance, so
            # a real finding read as a broken harness. ADR D3 already says a distiller raising
            # is a deferral plus a FLAG, never a crash; the tier should agree.
            proposal = distiller.distill(
                SYNTHETIC_JOURNAL, StoreState(), adapter.distillation_prompt
            )
            apply_consolidation(store, adapter, proposal, session=f"live-{i}", batch="b1", expected_fingerprint=UNCHECKED)
        except GateFailure:
            rejected += 1

    rate = rejected / ROUNDS
    assert rate <= MAX_REJECTION_RATE, (
        f"{rejected}/{ROUNDS} live consolidations were rejected. The model and the gates disagree "
        "more than tolerance allows — suspect the distillation prompt before the gates."
    )


@requires_live
def test_a_second_session_does_not_erode_the_first(tmp_path):
    """The failure a stub cannot show: a real model quietly dropping what it did not re-observe."""
    adapter = _resolve(ADAPTER_REF)
    distiller = _resolve(DISTILLER_REF)
    store = MemoryStore(tmp_path / "store")
    queue = Queue(tmp_path / "queue", retention=adapter.retention)

    first = distiller.distill(SYNTHETIC_JOURNAL, StoreState(), adapter.distillation_prompt)
    apply_consolidation(store, adapter, first, session="live-1", batch="b1", expected_fingerprint=UNCHECKED)
    queue.mark_consolidated("live-1")

    from memento import current_state

    second = distiller.distill(
        [{"turn": 1, "said": "Today I just want to talk about bread."}],
        current_state(store, adapter),
        adapter.distillation_prompt,
    )
    apply_consolidation(store, adapter, second, session="live-2", batch="b2", expected_fingerprint=UNCHECKED)
