"""A synthetic consumer, importable by reference — what the detached drain child resolves.

Stands in for a real app's adapter module. The distiller is recorded output, not a model, so the
subprocess path is exercised end-to-end with no credentials and no token spend.
"""

from __future__ import annotations

from typing import Any

from conftest import base_facts, make_adapter
from memento import Proposal, StoreState

ADAPTER = make_adapter()


class RecordedDistiller:
    def distill(self, journal: list[dict[str, Any]], state: StoreState, prompt: str) -> Proposal:
        facts = base_facts()
        facts["interests"].append(
            {"topic": "subprocess", "engagement": "low", "notes": "written by the drain child"}
        )
        return Proposal(
            facts=facts,
            entries={"vocab/en": [{"id": "v-drained", "item": "drained"}]},
            session_log="consolidated by the detached child",
        )


DISTILLER = RecordedDistiller()
