"""Live FalkorDB proof of the split group, applied by the real gateway.

The fast rig keys nodes by label and id and matches edges by the fake applier's rules, so it
proves neither that FalkorDB keeps a `Capability` and a `ResolvedCapability` with the same id
apart, nor that an edge a pre-gateway writer left without an identity is stamped and then deleted
by identity. Here the real split reads, guard, group builder and `GraphWriteGateway` (over the
in-memory log store) run against a real server: a single-tenant graph holding the marker, its
terminal Capability with identity-less and identity-carrying edges and a governing Policy, and a
real `{short}_baseline` graph holding X. The final state is read back with plain Cypher.

Run with:
`PS_TEST_FALKORDB_HOST=127.0.0.1 PS_TEST_FALKORDB_PORT=16379
uv run pytest ps-service/tests/graph_cleanup/test_graph_writer_capability_split_live.py
-m falkordb_live -q`
Uses uniquely named throwaway graphs, deleted after each test.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from graph_gateway._fakes import InMemoryGraphLogStore
from graph_gateway.live_endpoints import FALKORDB_TEST_TIMEOUTS, falkordb_endpoint

from ps_service.graph_cleanup.errors import GraphCleanupStaleStateError
from ps_service.graph_cleanup.graph_reader import (
    read_capability_present,
    read_resolved_capability_present,
)
from ps_service.graph_cleanup.graph_submission import GraphWriteTarget, cleanup_submitter
from ps_service.graph_cleanup.group_builders import write_capability_split
from ps_service.graph_cleanup.split_planner import plan_capability_split
from ps_service.graph_cleanup.split_reader import read_capability_split_state
from ps_service.graph_gateway.connection import FalkorDB, connect, select_graph
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.models import MutationGroup, UpsertNode

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.graph_gateway.connection import WritableGraph as GraphHandle

pytestmark = pytest.mark.falkordb_live

_X = "cap_x_vuln_patch"
_A = "cap_a_vuln_remediation"
_POLICY = "pol_patching"
_AUDIT_EVENT_ID = "6c1e2f3a-4b5d-4e6f-8a9b-0c1d2e3f4a5b"
_SNAPSHOT_QUERY = (
    "MATCH (n) OPTIONAL MATCH (n)-[r]->(m) "
    "RETURN labels(n), n.id, type(r), r.identity, m.id ORDER BY n.id, type(r), m.id"
)


@dataclass(frozen=True)
class _World:
    db: FalkorDB
    name: str
    baseline_name: str
    gateway: GraphWriteGateway

    @property
    def graph(self) -> GraphHandle:
        return select_graph(self.db, self.name)

    @property
    def baselines(self) -> tuple[tuple[str, GraphHandle], ...]:
        return ((self.baseline_name, select_graph(self.db, self.baseline_name)),)

    def target(self) -> GraphWriteTarget:
        return GraphWriteTarget(cleanup_submitter(self.gateway), self.name)

    def rows(self, query: str, params: dict[str, object] | None = None) -> list[list[object]]:
        return cast("list[list[object]]", self.graph.query(query, params).result_set)


def _link(graph: GraphHandle, rel_type: str, label: str, source: str, target: str) -> None:
    """Create an edge as a pre-gateway writer left it: no `identity` property."""
    graph.query(
        f"MATCH (s:{label} {{id: $s}}), (c:Capability {{id: $c}}) CREATE (s)-[:{rel_type}]->(c)",
        {"s": source, "c": target},
    )


def _seed(world: _World) -> None:
    graph = world.graph
    graph.query(
        "CREATE (:Capability {id: $a, name: 'Vulnerability remediation', status: 'active', "
        "confidence: 0.8}), (:ResolvedCapability {id: $x, resolves_to: $a}), "
        "(:Policy {id: $p, title: 'Patch Management Policy', status: 'approved'}), "
        "(:Obligation {id: 'obl_moves'}), (:Obligation {id: 'obl_shared'}), "
        "(:Obligation {id: 'obl_stays'}), (:Obligation {id: 'obl_added'}), "
        "(:PracticeArea {id: 'pa_moves'}), (:RiskPath {id: 'rp_stays'})",
        {"a": _A, "x": _X, "p": _POLICY},
    )
    graph.query(
        "MATCH (c:Capability {id: $a}), (p:Policy {id: $p}) CREATE (c)-[:GOVERNED_BY]->(p)",
        {"a": _A, "p": _POLICY},
    )
    for rel_type, label, source in (
        ("REQUIRES", "Obligation", "obl_moves"),
        ("REQUIRES", "Obligation", "obl_shared"),
        ("REQUIRES", "Obligation", "obl_stays"),
        ("COVERS", "PracticeArea", "pa_moves"),
        ("MITIGATED_BY", "RiskPath", "rp_stays"),
    ):
        _link(graph, rel_type, label, source, _A)
    world.gateway.submit_group(
        MutationGroup(
            graph=world.baseline_name,
            audit_event_id=_AUDIT_EVENT_ID,
            primitives=(
                UpsertNode(label="RegulatoryInstrument", id="32022L2555", properties={}),
                UpsertNode(
                    label="Capability",
                    id=_X,
                    properties={"name": "Vulnerability patching", "confidence": 0.9},
                    embedding=(1.0, 0.0),
                ),
                UpsertNode(label="Capability", id=_A, properties={"name": "Remediation"}),
                *(
                    UpsertNode(label=label, id=source, properties={})
                    for label, source in (
                        ("Obligation", "obl_moves"),
                        ("Obligation", "obl_shared"),
                        ("Obligation", "obl_added"),
                        ("PracticeArea", "pa_moves"),
                    )
                ),
            ),
        )
    )
    baseline = world.baselines[0][1]
    for rel_type, label, source, target in (
        ("REQUIRES", "Obligation", "obl_moves", _X),
        ("REQUIRES", "Obligation", "obl_shared", _X),
        ("REQUIRES", "Obligation", "obl_added", _X),
        ("REQUIRES", "Obligation", "obl_shared", _A),
        ("COVERS", "PracticeArea", "pa_moves", _X),
    ):
        _link(baseline, rel_type, label, source, target)


@pytest.fixture
def world() -> Iterator[_World]:
    host, port = falkordb_endpoint()
    db = connect(host=host, port=port, **FALKORDB_TEST_TIMEOUTS)
    suffix = uuid.uuid4().hex[:8]
    name = f"graph_cleanup_split_live_{suffix}"
    baseline_name = f"graph_cleanup_split_live_{suffix}_baseline"
    gateway = GraphWriteGateway(
        log_store=InMemoryGraphLogStore(), graph_opener=lambda graph: select_graph(db, graph)
    )
    built = _World(db=db, name=name, baseline_name=baseline_name, gateway=gateway)
    _seed(built)
    try:
        yield built
    finally:
        for graph_name in (name, baseline_name):
            if graph_name in db.list_graphs():
                db.select_graph(graph_name).delete()


def _sources(world: _World, rel_type: str, target: str) -> list[str]:
    rows = world.rows(
        f"MATCH (s)-[:{rel_type}]->(c:Capability {{id: $c}}) RETURN s.id ORDER BY s.id",
        {"c": target},
    )
    return [cast("str", row[0]) for row in rows]


def _split(world: _World, *, state_digest: str | None = None) -> None:
    state = read_capability_split_state(world.graph, world.baselines, _X)
    digest = state_digest or plan_capability_split(state).state_digest
    write_capability_split(
        world.graph,
        world.baselines,
        world.target(),
        audit_event_id=_AUDIT_EVENT_ID,
        x_id=_X,
        state_digest=digest,
        created_at=datetime.now(UTC).isoformat(),
    )


def test_live_split_mints_x_beside_its_marker_then_deletes_only_the_marker(
    world: _World,
) -> None:
    a_before = world.rows("MATCH (c:Capability {id: $a}) RETURN properties(c)", {"a": _A})
    assert read_resolved_capability_present(world.graph, capability_id=_X) is True
    assert read_capability_present(world.graph, capability_id=_X) is False

    _split(world)

    assert world.rows(
        "MATCH (c:Capability {id: $x}) RETURN c.name, c.confidence, c.embedding IS NOT NULL",
        {"x": _X},
    ) == [["Vulnerability patching", 0.9, True]]
    assert world.rows("MATCH (m:ResolvedCapability {id: $x}) RETURN count(m)", {"x": _X}) == [[0]]
    assert read_resolved_capability_present(world.graph, capability_id=_X) is False
    assert read_capability_present(world.graph, capability_id=_X) is True
    assert world.rows("MATCH (c:Capability {id: $a}) RETURN properties(c)", {"a": _A}) == a_before


def test_live_split_moves_the_baseline_held_edges_and_leaves_governance_on_a(
    world: _World,
) -> None:
    _split(world)

    assert _sources(world, "REQUIRES", _X) == ["obl_added", "obl_moves", "obl_shared"]
    assert _sources(world, "REQUIRES", _A) == ["obl_shared", "obl_stays"]
    assert _sources(world, "COVERS", _X) == ["pa_moves"]
    assert _sources(world, "COVERS", _A) == []
    assert _sources(world, "MITIGATED_BY", _X) == []
    assert _sources(world, "MITIGATED_BY", _A) == ["rp_stays"]
    assert world.rows(
        "MATCH (c:Capability)-[:GOVERNED_BY]->(p:Policy) RETURN c.id, p.id ORDER BY c.id"
    ) == [[_A, _POLICY]]
    assert world.rows(
        "MATCH ()-[r:REQUIRES|COVERS|MITIGATED_BY]->(:Capability) "
        "WHERE r.identity IS NULL RETURN count(r)"
    ) == [[0]]


def test_live_a_stale_digest_leaves_the_graph_identical(world: _World) -> None:
    before = world.rows(_SNAPSHOT_QUERY)

    with pytest.raises(GraphCleanupStaleStateError):
        _split(world, state_digest="0" * 64)

    assert world.rows(_SNAPSHOT_QUERY) == before
