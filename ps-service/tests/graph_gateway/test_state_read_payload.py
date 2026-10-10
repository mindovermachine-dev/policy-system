"""The reads that precede a write do not drag node payloads over the wire (#207 S19).

Found by the CRA x10 load.

A group of edges used to read the full state of every endpoint, `properties(n)` with its
3,072-double embedding included, only to prove the endpoint exists. 500 such rows took 1.1 s on a
real FalkorDB, past its default 1000 ms query timeout, so a bulk load failed with
`Query timed out`. Endpoints that the group does not otherwise act on are now checked by id only,
and the reads that do need the state are bounded in rows per statement.
"""

from __future__ import annotations

import pytest

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.errors import MissingTargetError
from ps_service.graph_gateway.gateway import GatewaySettings
from ps_service.graph_gateway.graph_reader import STATE_READ_CHUNK_ROWS
from ps_service.graph_gateway.label_allow_list import ALLOWED_RELATIONSHIP_TYPES
from ps_service.graph_gateway.models import (
    DeleteEdge,
    MutationGroup,
    NodeRef,
    Primitive,
    UpsertEdge,
    UpsertNode,
)

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_EMBEDDING = (0.25, -0.5, 0.125)


def _edge_type() -> str:
    return min(ALLOWED_RELATIONSHIP_TYPES)


def _submit(rig: GatewayRig, *primitives: Primitive) -> None:
    rig.gateway.submit_group(
        MutationGroup(graph=_GRAPH, audit_event_id=_AUDIT_EVENT_ID, primitives=primitives)
    )


def _edge(source: str, target: str) -> UpsertEdge:
    return UpsertEdge(
        type=_edge_type(),
        identity=f"{source}-{target}",
        source=NodeRef(label="Capability", id=source),
        target=NodeRef(label="Capability", id=target),
    )


def _read_templates(rig: GatewayRig) -> list[str]:
    return [q.template for q in rig.graphs.open(_GRAPH).queries if q.kind == "read"]


def _with_nodes(count: int) -> GatewayRig:
    rig = GatewayRig(settings=GatewaySettings(batch_size=500))
    _submit(
        rig,
        *(
            UpsertNode(label="Capability", id=f"cap-{n}", embedding=_EMBEDDING)
            for n in range(count)
        ),
    )
    rig.graphs.open(_GRAPH).queries.clear()
    return rig


def test_edge_endpoints_are_checked_by_id_without_reading_their_state() -> None:
    rig = _with_nodes(3)

    _submit(rig, _edge("cap-0", "cap-1"), _edge("cap-1", "cap-2"))

    templates = _read_templates(rig)
    assert "node_state" not in templates  # no properties(n), no embedding on the wire
    assert "node_exists" in templates


def test_an_edge_to_a_missing_endpoint_is_still_rejected() -> None:
    rig = _with_nodes(1)

    with pytest.raises(MissingTargetError):
        _submit(rig, _edge("cap-0", "cap-missing"))


def test_an_edge_to_a_node_created_earlier_in_the_same_group_is_accepted() -> None:
    rig = _with_nodes(1)

    _submit(rig, UpsertNode(label="Capability", id="cap-new"), _edge("cap-0", "cap-new"))

    assert ("Capability", "cap-new") in rig.graphs.open(_GRAPH).nodes


def test_an_edge_endpoint_that_the_group_also_changes_is_read_in_full() -> None:
    rig = _with_nodes(2)

    # cap-0 is both an endpoint and upserted again with the very same content: a no-op, which
    # only a full read of its state can tell.
    outcome = rig.gateway.submit_group(
        MutationGroup(
            graph=_GRAPH,
            audit_event_id=_AUDIT_EVENT_ID,
            primitives=(
                UpsertNode(label="Capability", id="cap-0", embedding=_EMBEDDING),
                _edge("cap-0", "cap-1"),
            ),
        )
    )

    assert "node_state" in _read_templates(rig)
    assert (outcome.first_position, outcome.last_position) == (3, 3)  # only the edge was logged


def test_deleting_an_edge_does_not_read_its_endpoints_state() -> None:
    rig = _with_nodes(2)
    _submit(rig, _edge("cap-0", "cap-1"))
    rig.graphs.open(_GRAPH).queries.clear()

    _submit(
        rig,
        DeleteEdge(
            type=_edge_type(),
            identity="cap-0-cap-1",
            source=NodeRef(label="Capability", id="cap-0"),
            target=NodeRef(label="Capability", id="cap-1"),
        ),
    )

    assert "node_state" not in _read_templates(rig)


def test_state_reads_carry_at_most_the_chunk_bound_of_rows_per_statement() -> None:
    count = STATE_READ_CHUNK_ROWS * 2 + 5
    rig = _with_nodes(count)

    # Upserting the same nodes again needs their full state, in bounded statements.
    _submit(
        rig,
        *(
            UpsertNode(label="Capability", id=f"cap-{n}", embedding=_EMBEDDING)
            for n in range(count)
        ),
    )

    rows = [
        len(q.params["rows"])  # pyright: ignore[reportArgumentType]
        for q in rig.graphs.open(_GRAPH).queries
        if q.template == "node_state"
    ]
    assert sum(rows) == count
    assert max(rows) <= STATE_READ_CHUNK_ROWS
