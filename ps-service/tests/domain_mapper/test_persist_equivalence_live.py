"""`falkordb_live` + `postgres_live`: the Domain Mapper's gateway write path, real stores (#209).

The live twin of `test_persist_equivalence.py`: the same recorded-CRA inputs go through the frozen
pre-#209 Cypher (`_legacy_reference`, run against a real FalkorDB graph) and through the real
`persist_*` functions over the real gateway (the `PsycopgGraphLogStore` as the `ps_state` role on a
scratch PS Postgres, and the real FalkorDB opener), each on a uniquely named graph that is deleted
afterwards.

AC-BI-002 is met modulo identity (D2): the digest of the graph the gateway built equals the digest
of the graph the legacy Cypher built, once the legacy edges are given the gateway's deterministic
`identity` (the one stamp Cypher below). A second test shows an edge whose endpoint exists nowhere
is dropped by both paths (AC-BI-004). Met modulo identity; live digest not verified here: this
sandbox has no FalkorDB module and no Postgres, so these tests are only collected and
linted/type-checked in the implementation; the fast, in-memory twin carries the verified
assertions.

To run: start a FalkorDB and a scratch Postgres, then
`PS_TEST_FALKORDB_HOST=127.0.0.1 PS_TEST_FALKORDB_PORT=16379
PS_TEST_POSTGRES_SUPERUSER_DSN=postgresql://postgres@127.0.0.1:54329/postgres
uv run pytest ps-service/tests/domain_mapper/test_persist_equivalence_live.py
-m "falkordb_live and postgres_live"`.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest
from graph_gateway.live_endpoints import falkordb_endpoint
from graph_gateway.live_postgres import committed_audit_event

from domain_mapper._legacy_reference import (
    write_legacy_obligation_and_capability_graph,
    write_legacy_role_and_requirement_graph,
)
from domain_mapper._recorded_inputs import SUBSET_FIXTURE, Inputs, recorded_inputs
from ps_service.domain_mapper.graph_writer import (
    persist_obligation_and_capability_graph,
    persist_role_and_requirement_graph,
)
from ps_service.domain_mapper.models import (
    CapabilityRequiresEdge,
    ObligationHasEdge,
    RequirementSatisfiedByEdge,
)
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.store import PsycopgGraphLogStore
from ps_service.ingestion.falkordb_client import connect, select_graph

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persistence.provisioned_postgres import Provisioned

    from ps_service.ingestion.falkordb_client import FalkorDB, GraphHandle

pytestmark = [pytest.mark.falkordb_live, pytest.mark.postgres_live]

_INSTRUMENT_ID = "CRA-1.0"
_INSTRUMENT_PROPERTIES: dict[str, object] = {
    "id": "CRA-1.0",
    "title": "Cyber Resilience Act",
    "version": "1.0",
}

# Gives every legacy edge the identity the gateway computes (`TYPE:source->target`); the
# test-side stand-in for "the legacy graph, seen through the gateway" (D2).
_STAMP_IDENTITY = (
    "MATCH (a)-[r]->(b) WHERE r.identity IS NULL "
    "SET r.identity = type(r) + ':' + a.id + '->' + b.id"
)
_EDGE_SHAPE = "MATCH (a)-[r]->(b) RETURN type(r), labels(a)[0], a.id, labels(b)[0], b.id"


class _Stack:
    """The real gateway, and the throwaway graphs of one test."""

    def __init__(self, prov: Provisioned, db: FalkorDB) -> None:
        self.prov = prov
        self.db = db
        self.gateway = GraphWriteGateway(
            log_store=PsycopgGraphLogStore(prov.state_config()),
            graph_opener=lambda graph: select_graph(db, graph),
        )
        self.names: list[str] = []

    def graph_name(self, kind: str) -> str:
        """A fresh graph name, deleted when the test ends."""
        name = f"dm_live_{uuid.uuid4().hex[:8]}_{kind}"
        self.names.append(name)
        return name

    def handle(self, name: str) -> GraphHandle:
        """The real graph of that name."""
        return select_graph(self.db, name)

    def digest(self, name: str) -> str:
        """The canonical digest of the real graph."""
        return canonical_digest(self.handle(name))

    def edge_shape(self, name: str) -> Counter[tuple[object, ...]]:
        """The edge multiset (type, endpoints), identity ignored."""
        rows = cast("list[list[object]]", self.handle(name).query(_EDGE_SHAPE).result_set)
        return Counter(tuple(row) for row in rows)

    def close(self) -> None:
        """Delete every graph the test used."""
        existing = set(self.db.list_graphs())
        for name in self.names:
            if name in existing:
                self.db.select_graph(name).delete()


@pytest.fixture
def stack(provisioned_graph_log: Provisioned) -> Iterator[_Stack]:
    host, port = falkordb_endpoint()
    made = _Stack(provisioned_graph_log, connect(host=host, port=port))
    yield made
    made.close()


def _write_legacy(stack: _Stack, inputs: Inputs) -> str:
    """The graph the pre-#209 writer built: its own Cypher on a real graph, edges stamped."""
    name = stack.graph_name("legacy_baseline")
    graph = stack.handle(name)
    write_legacy_role_and_requirement_graph(
        graph,
        _INSTRUMENT_ID,
        _INSTRUMENT_PROPERTIES,
        inputs.role_nodes,
        inputs.role_edges,
        inputs.requirement_nodes,
        inputs.requirement_edges,
    )
    write_legacy_obligation_and_capability_graph(
        graph,
        inputs.obligation_nodes,
        inputs.has_edges,
        inputs.satisfied_by_edges,
        inputs.capability_nodes,
        inputs.requires_edges,
    )
    graph.query(_STAMP_IDENTITY)
    return name


def _write_new(stack: _Stack, inputs: Inputs) -> str:
    """The graph the real `persist_*` functions build through the real gateway."""
    name = stack.graph_name("baseline")
    audit_event_id = committed_audit_event(stack.prov)
    persist_role_and_requirement_graph(
        stack.gateway,
        name,
        audit_event_id,
        regulatory_instrument_id=_INSTRUMENT_ID,
        regulatory_instrument_properties=_INSTRUMENT_PROPERTIES,
        role_nodes=inputs.role_nodes,
        role_edges=inputs.role_edges,
        requirement_nodes=inputs.requirement_nodes,
        requirement_edges=inputs.requirement_edges,
    )
    persist_obligation_and_capability_graph(
        stack.gateway,
        name,
        audit_event_id,
        graph=stack.handle(name),
        obligation_nodes=inputs.obligation_nodes,
        has_edges=inputs.has_edges,
        satisfied_by_edges=inputs.satisfied_by_edges,
        capability_nodes=inputs.capability_nodes,
        requires_edges=inputs.requires_edges,
    )
    return name


def test_recorded_cra_digest_equals_the_legacy_writers_modulo_edge_identity(
    stack: _Stack,
) -> None:
    inputs = recorded_inputs(SUBSET_FIXTURE)
    legacy = _write_legacy(stack, inputs)

    new = _write_new(stack, inputs)

    assert stack.digest(new) == stack.digest(legacy)
    assert stack.edge_shape(new) == stack.edge_shape(legacy)
    assert sum(stack.edge_shape(new).values()) > 0


def test_edges_whose_endpoint_exists_nowhere_are_dropped_by_both_paths(stack: _Stack) -> None:
    base = recorded_inputs(SUBSET_FIXTURE)
    requirement = base.requirement_nodes[0]
    obligation = base.obligation_nodes[0]
    inputs = replace(
        base,
        has_edges=(*base.has_edges, ObligationHasEdge("role_ghost", obligation.id)),
        satisfied_by_edges=(
            *base.satisfied_by_edges,
            RequirementSatisfiedByEdge("req_ghost", obligation.id),
            RequirementSatisfiedByEdge(requirement.id, "obl_ghost"),
        ),
        requires_edges=(*base.requires_edges, CapabilityRequiresEdge(obligation.id, "cap_ghost")),
    )
    legacy = _write_legacy(stack, inputs)

    new = _write_new(stack, inputs)

    assert stack.digest(new) == stack.digest(legacy)
    assert ("HAS", "Role", "role_ghost", "Obligation", obligation.id) not in stack.edge_shape(new)
