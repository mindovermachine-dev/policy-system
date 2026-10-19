"""Three incoming Capabilities, each a different distance below the merge threshold.

Three Capabilities are already in `policy_system`, on axes 0, 1 and 2 of an 8-dimensional space.
The incoming ones each lean on one of them and are otherwise on axes of their own, so they never
score against each other:

* `BELOW` scores 0.4 against its neighbour,
* `AT` scores exactly 0.5 (four components of 0.5 give a unit norm and a dot product of 0.5, all
  exactly representable),
* `ABOVE` scores 0.6.

With a merge threshold of `MERGE` (0.9) and a review floor of `FLOOR` (0.5), `BELOW` raises no
review and `AT` and `ABOVE` do. The same ids and vectors feed ingest (`merge_baseline_graph`) and
restore (`restore_instrument`), so both paths are checked against one expectation.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from litellm.types.utils import Embedding, EmbeddingResponse
from restore._fixtures import build_restore_artifact, small_graphs

from company_merge._gateway_fakes import FamilyABaseline
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode

if TYPE_CHECKING:
    from graph_gateway._fakes import InMemoryGraph

    from ps_service.restore.models import RestoreArtifact

Vector = tuple[float, ...]

MERGE = 0.9
FLOOR = 0.5
INSTRUMENT_ID = "NM349-1.0"
EMBED_MODEL = "fake-embed-model"

# id -> (embedding, name)
EXISTING: dict[str, tuple[Vector, str]] = {
    "cap_ex_below": ((1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0), "Keep Records"),
    "cap_ex_at": ((0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0), "Patch Fast"),
    "cap_ex_above": ((0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0), "Test Backups"),
}
# id -> (embedding, name, nearest existing id, expected similarity)
INCOMING: dict[str, tuple[Vector, str, str, float]] = {
    "cap_in_below": (
        (0.4, 0.0, 0.0, math.sqrt(1.0 - 0.4 * 0.4), 0.0, 0.0, 0.0, 0.0),
        "Retain Records",
        "cap_ex_below",
        0.4,
    ),
    "cap_in_at": (
        (0.0, 0.5, 0.0, 0.0, 0.5, 0.5, 0.5, 0.0),
        "Patch Quickly",
        "cap_ex_at",
        0.5,
    ),
    "cap_in_above": (
        (0.0, 0.0, 0.6, 0.0, 0.0, 0.0, 0.0, 0.8),
        "Verify Backups",
        "cap_ex_above",
        0.6,
    ),
}
BELOW_ID, AT_ID, ABOVE_ID = "cap_in_below", "cap_in_at", "cap_in_above"


def seed_existing(graph: InMemoryGraph) -> None:
    """Put the three Capabilities the incoming ones lean on into `policy_system`."""
    for cap_id, (embedding, name) in EXISTING.items():
        graph.nodes[("Capability", cap_id)] = {
            "id": cap_id,
            "name": name,
            "embedding": list(embedding),
            "status": "active",
        }


def restore_artifact() -> RestoreArtifact:
    """The small external instrument plus the three incoming Capabilities, with embeddings."""
    native, baseline = small_graphs(instrument_id=INSTRUMENT_ID)
    capabilities = tuple(
        SerializedNode(
            "Capability",
            {"id": cap_id, "name": name, "confidence": 0.8, "embedding": list(vector)},
        )
        for cap_id, (vector, name, _, _) in INCOMING.items()
    )
    requires = tuple(
        SerializedEdge("REQUIRES", "Obligation", "obl_1", "Capability", cap_id, {})
        for cap_id in INCOMING
    )
    graph = SerializedGraph(
        nodes=(*baseline.nodes, *capabilities), edges=(*baseline.edges, *requires)
    )
    return build_restore_artifact(
        instrument_id=INSTRUMENT_ID,
        short_name="NM349",
        native_graph=native,
        baseline_graph=graph,
    )


def ingest_baseline(*cap_ids: str) -> FamilyABaseline:
    """The `{short}_baseline` of the same instrument holding `cap_ids` (all when none given)."""
    chosen = cap_ids or tuple(INCOMING)
    return FamilyABaseline(
        instrument_id=INSTRUMENT_ID,
        instrument_properties={"id": INSTRUMENT_ID, "title": "NM349"},
        capabilities={cap_id: (INCOMING[cap_id][1], 0.8) for cap_id in chosen},
    )


class ByNameEmbedding:
    """An `EmbeddingCaller` answering each Capability name with its scripted vector."""

    def __init__(self, extra: dict[str, Vector] | None = None) -> None:
        self._by_name = {name: vector for vector, name, _, _ in INCOMING.values()}
        self._by_name.update(extra or {})
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
