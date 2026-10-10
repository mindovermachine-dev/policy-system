"""64-bit floats survive log, replay and graph bit for bit (#207 S14, AC-RD-007).

Embeddings travel as little-endian binary64 payloads; float properties travel inside the entry's
JSON content. The hermetic proof goes through the real entry codec and payload codec with the
in-memory store (which round-trips content through JSON as Postgres does); whether the real
`jsonb` column keeps `-0.0` and `1.0` is the live twin (`test_replay_live.py`).
"""

from __future__ import annotations

from typing import cast

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.models import MutationGroup, UpsertNode

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_EMBEDDING = (0.1, -0.0, 1.7976931348623157e308, 5e-324, 0.30000000000000004, 1 / 3)
_PROPERTIES: dict[str, object] = {
    "negative_zero": -0.0,
    "one": 1.0,
    "big": 1e22,
    "tiny": 5e-324,
    "tenth": 0.1,
    "exact_int": 2**53,
    "int_max": 2**63 - 1,
    "series": [0.1, -0.0, 1.0],
}


def test_replayed_embeddings_are_bit_identical_through_the_log_codec() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(
        MutationGroup(
            graph=_GRAPH,
            audit_event_id=_AUDIT_EVENT_ID,
            primitives=(
                UpsertNode(label="Capability", id="cap-1", properties={}, embedding=_EMBEDDING),
            ),
            checkpoint_requested=True,
        )
    )
    graph = rig.graphs.open(_GRAPH)
    before = canonical_digest(graph)
    graph.flush()

    report = rig.restart().replay_graph(_GRAPH)

    replayed = cast("list[float]", graph.nodes[("Capability", "cap-1")]["embedding"])
    assert [value.hex() for value in replayed] == [value.hex() for value in _EMBEDDING]
    assert report.verified_position == 1  # the checkpoint taken before the wipe vouches for it
    assert canonical_digest(graph) == before


def test_replayed_float_and_int_properties_keep_their_type_and_bits() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(
        MutationGroup(
            graph=_GRAPH,
            audit_event_id=_AUDIT_EVENT_ID,
            primitives=(UpsertNode(label="Capability", id="cap-1", properties=_PROPERTIES),),
            checkpoint_requested=True,
        )
    )
    graph = rig.graphs.open(_GRAPH)
    before = canonical_digest(graph)
    graph.flush()

    rig.restart().replay_graph(_GRAPH)

    stored = graph.nodes[("Capability", "cap-1")]
    assert repr(stored["negative_zero"]) == "-0.0"
    assert isinstance(stored["one"], float)
    assert isinstance(stored["exact_int"], int)
    assert not isinstance(stored["exact_int"], bool)
    assert stored["exact_int"] == 2**53
    assert canonical_digest(graph) == before
