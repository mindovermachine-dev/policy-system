"""Scripted Capability vectors where a later, closer Capability would win a fresh scoring.

`A` is already in `policy_system`. The incoming `X` scores 0.93 against `A`, above the merge
threshold `MERGE` (0.9), so the first ingest resolves `X` onto `A`. An unrelated regulation then
brings `B`, which scores 0.8 against `A` and is minted. `X` scores about 0.965 against `B`, closer
than `A`, so an amended version that scored `X` again would pick `B`; following the recorded
resolution keeps it on `A`.

All vectors are unit length in two dimensions:

* `X·A = 0.93 >= MERGE`,
* `B·A = 0.8 < MERGE`,
* `X·B ≈ 0.965 > X·A`.

The same ids, names and vectors feed every path that has to land `X` on `A` (ingest and restore).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from litellm.types.utils import Embedding, EmbeddingResponse

from company_merge._gateway_fakes import FamilyABaseline
from ps_service.domain_mapper.identity import capability_id, obligation_id, role_id

if TYPE_CHECKING:
    from graph_gateway._fakes import InMemoryGraph

Vector = tuple[float, ...]

MERGE = 0.9
EMBED_MODEL = "fake-embed-model"

TARGET_NAME = "Incident Notification"
RESOLVED_NAME = "Incident Reporting"
NEWER_NAME = "Incident Escalation"

TARGET_ID = capability_id(TARGET_NAME)
RESOLVED_ID = capability_id(RESOLVED_NAME)
NEWER_ID = capability_id(NEWER_NAME)

TARGET_VECTOR: Vector = (1.0, 0.0)
NEWER_VECTOR: Vector = (0.8, 0.6)
RESOLVED_VECTOR: Vector = (0.93, math.sqrt(1.0 - 0.93 * 0.93))

FIRST_VERSION = "AMEND-1.0"
AMENDED_VERSION = "AMEND-2.0"
UNRELATED_INSTRUMENT = "OTHER-1.0"


def seed_target(graph: InMemoryGraph) -> None:
    """Put the Capability `A` the first version resolves onto into `policy_system`."""
    graph.seed_node(
        ("Capability",), TARGET_ID, {"name": TARGET_NAME, "embedding": list(TARGET_VECTOR)}
    )


def requiring_baseline(instrument_id: str, capability: str, name: str) -> FamilyABaseline:
    """One Role of `instrument_id` with one Obligation that `REQUIRES` Capability `capability`.

    The Role and Obligation ids are derived from the instrument id, so each version has its own.
    """
    role = role_id("Operator", instrument_id)
    obligation = obligation_id(role, f"Report incidents under {instrument_id}.")
    return FamilyABaseline(
        instrument_id=instrument_id,
        instrument_properties={"id": instrument_id, "title": instrument_id},
        roles={role: "Operator"},
        obligations={obligation: (f"Report incidents under {instrument_id}.", 0.9)},
        capabilities={capability: (name, 0.8)},
        edges=(
            ("HAS", "Role", role, "Obligation", obligation),
            ("REQUIRES", "Obligation", obligation, "Capability", capability),
        ),
    )


def version_baseline(instrument_id: str) -> FamilyABaseline:
    """A version of the regulation that regenerates the resolved Capability `X`."""
    return requiring_baseline(instrument_id, RESOLVED_ID, RESOLVED_NAME)


def unrelated_baseline() -> FamilyABaseline:
    """An unrelated regulation that brings the closer Capability `B`."""
    return requiring_baseline(UNRELATED_INSTRUMENT, NEWER_ID, NEWER_NAME)


def obligation_of(baseline: FamilyABaseline) -> str:
    """The one Obligation id of a baseline built here."""
    [obligation] = baseline.obligations
    return obligation


class ByNameEmbedding:
    """An `EmbeddingCaller` answering each Capability name with its scripted vector."""

    def __init__(self) -> None:
        self._by_name = {
            TARGET_NAME: TARGET_VECTOR,
            RESOLVED_NAME: RESOLVED_VECTOR,
            NEWER_NAME: NEWER_VECTOR,
        }
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        """Embed the one text of a dedup call."""
        del timeout
        text = inputs[0]
        self.calls.append(text)
        return EmbeddingResponse(
            model=model,
            data=[Embedding(embedding=list(self._by_name[text]), index=0, object="embedding")],
        )
