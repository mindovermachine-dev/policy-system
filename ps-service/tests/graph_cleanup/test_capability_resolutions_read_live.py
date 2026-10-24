"""Live FalkorDB proof of the reads behind `list-capability-resolutions` and `split-capability`.

The fast doubles answer by statement text and key nodes by label and id, so they prove neither the
label, direction and parameters of a read nor what FalkorDB does with an unlisted graph name.
These tests run the real Company Merge marker reads, the real `graph_reader` and `baseline_reader`
reads and the real baseline opener against a real server: a single-tenant graph holding a
`ResolvedCapability` chained through a `MergedCapability`, cached embeddings, Obligations (one
absorbed by a `MergedObligation`), the edges the resolution put on its terminal, and real
`{short}_baseline` graphs with `REQUIRES`, `COVERS` and `MITIGATED_BY` edges.

Run with:
`PS_TEST_FALKORDB_HOST=127.0.0.1 PS_TEST_FALKORDB_PORT=16379
uv run pytest ps-service/tests/graph_cleanup/test_capability_resolutions_read_live.py
-m falkordb_live -q`
Uses uniquely named throwaway graphs, deleted after each test.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from graph_gateway.live_endpoints import FALKORDB_TEST_TIMEOUTS, falkordb_endpoint

from ps_service.config import load_config
from ps_service.graph_cleanup.baseline_reader import (
    read_baseline_capabilities,
    read_baseline_incoming_edges,
    read_baseline_instrument_ids,
)
from ps_service.graph_cleanup.dependencies import build_default_graph_cleanup_baseline_opener
from ps_service.graph_cleanup.graph_reader import (
    read_capability_resolutions,
    read_existing_sources,
    read_obligation_merge_markers,
    read_resolution_terminals,
)
from ps_service.graph_cleanup.models import CapabilityResolution
from ps_service.graph_cleanup.service import list_capability_resolutions, preview_capability_split
from ps_service.graph_gateway.connection import connect, select_graph

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.config import ServiceConfig
    from ps_service.graph_gateway.connection import WritableGraph as GraphHandle

pytestmark = pytest.mark.falkordb_live


@dataclass(frozen=True)
class _World:
    config: ServiceConfig
    graph: GraphHandle
    baselines: tuple[tuple[str, GraphHandle], ...]
    x_id: str
    y_id: str
    a_id: str
    b_id: str
    d_id: str


@pytest.fixture
def world() -> Iterator[_World]:
    host, port = falkordb_endpoint()
    db = connect(host=host, port=port, **FALKORDB_TEST_TIMEOUTS)
    tag = uuid.uuid4().hex[:8]
    single = f"graph_cleanup_resolutions_live_{tag}"
    first = f"rslive{tag}a_baseline"
    second = f"rslive{tag}b_baseline"
    ids = {
        "x": f"cap_x_{tag}",
        "y": f"cap_y_{tag}",
        "a": f"cap_a_{tag}",
        "b": f"cap_b_{tag}",
        "d": f"cap_d_{tag}",
    }
    graph = select_graph(db, single)
    graph.query(
        "CREATE (a:Capability {id: $a, name: 'Vulnerability remediation', status: 'active', "
        "embedding: [0.6, 0.8]}), "
        "(:Capability {id: $b, name: 'Asset inventory'}), "
        "(p:Policy {id: $policy, title: 'Patch Management Policy', status: 'approved'}), "
        "(a)-[:GOVERNED_BY]->(p), "
        "(:ResolvedCapability {id: $x, resolves_to: $a}), "
        "(:ResolvedCapability {id: $y, resolves_to: $d}), "
        "(:MergedCapability {id: $d, merged_into: $b}), "
        "(:Obligation {id: 'obl_direct'}), (:Obligation {id: 'obl_kept'}), "
        "(:MergedObligation {id: 'obl_merged', merged_into: 'obl_kept'})",
        {**ids, "policy": f"pol_{tag}"},
    )
    graph.query(
        "MATCH (a:Capability {id: $a}), (o:Obligation {id: 'obl_direct'}) "
        "CREATE (o)-[:REQUIRES]->(a), "
        "(:PracticeArea {id: 'pa_x'})-[:COVERS]->(a), "
        "(:RiskPath {id: 'rp_a'})-[:MITIGATED_BY]->(a)",
        {"a": ids["a"]},
    )
    held_first = select_graph(db, first)
    held_first.query(
        "CREATE (:RegulatoryInstrument {id: $instrument}), "
        "(:Capability {id: $x, name: 'vulnerability patching', confidence: 0.8})",
        {"instrument": f"inst_{tag}_a", "x": ids["x"]},
    )
    held_second = select_graph(db, second)
    held_second.query(
        "CREATE (:RegulatoryInstrument {id: $instrument}), "
        "(:Capability {id: $x, name: 'Vulnerability patching', confidence: 0.9, "
        "embedding: [1.0, 0.0]}), "
        "(:Capability {id: $y, name: 'Asset register', confidence: 0.7}), "
        "(:Capability {id: $a, name: 'Vulnerability remediation'}), "
        "(o1:Obligation {id: 'obl_direct'}), (o2:Obligation {id: 'obl_merged'}), "
        "(o3:Obligation {id: 'obl_gone'}), (o4:Obligation {id: 'obl_on_a'})",
        {"instrument": f"inst_{tag}_b", "x": ids["x"], "y": ids["y"], "a": ids["a"]},
    )
    held_second.query(
        "MATCH (x:Capability {id: $x}), (a:Capability {id: $a}), "
        "(o1:Obligation {id: 'obl_direct'}), (o2:Obligation {id: 'obl_merged'}), "
        "(o3:Obligation {id: 'obl_gone'}), (o4:Obligation {id: 'obl_on_a'}) "
        "CREATE (o1)-[:REQUIRES]->(x), (o2)-[:REQUIRES]->(x), (o3)-[:REQUIRES]->(x), "
        "(x)-[:REQUIRES]->(a), (o4)-[:REQUIRES]->(a)",
        {"x": ids["x"], "a": ids["a"]},
    )
    held_second.query(
        "MATCH (x:Capability {id: $x}), (a:Capability {id: $a}) "
        "CREATE (:PracticeArea {id: 'pa_x'})-[:COVERS]->(x), "
        "(x)-[:COVERS]->(:PracticeArea {id: 'pa_reversed'}), "
        "(:RiskPath {id: 'rp_a'})-[:MITIGATED_BY]->(a), "
        "(:Obligation {id: 'obl_wrong_type'})-[:COVERS]->(x)",
        {"x": ids["x"], "a": ids["a"]},
    )
    config = dataclasses.replace(load_config(), falkordb_host=host, falkordb_port=port)
    try:
        yield _World(
            config=config,
            graph=graph,
            baselines=((first, held_first), (second, held_second)),
            x_id=ids["x"],
            y_id=ids["y"],
            a_id=ids["a"],
            b_id=ids["b"],
            d_id=ids["d"],
        )
    finally:
        for name in (single, first, second):
            if name in db.list_graphs():
                db.select_graph(name).delete()


def test_live_resolution_markers_are_read_and_followed_through_a_cleanup_merge(
    world: _World,
) -> None:
    resolutions = read_capability_resolutions(world.graph)

    assert resolutions == (
        CapabilityResolution(
            x_id=world.x_id,
            marker_resolves_to=world.a_id,
            a_id=world.a_id,
            a_aliases=(world.a_id,),
        ),
        CapabilityResolution(
            x_id=world.y_id,
            marker_resolves_to=world.d_id,
            a_id=world.b_id,
            a_aliases=tuple(sorted((world.b_id, world.d_id))),
        ),
    )


def test_live_terminals_carry_name_and_governing_policy(world: _World) -> None:
    terminals = read_resolution_terminals(world.graph, {world.a_id, world.b_id, "cap_absent"})

    assert set(terminals) == {world.a_id, world.b_id}
    governing = terminals[world.a_id].governing_policy
    assert governing is not None
    assert (governing.title, governing.status) == ("Patch Management Policy", "approved")
    assert terminals[world.b_id].governing_policy is None
    assert terminals[world.a_id].embedding == pytest.approx((0.6, 0.8))
    assert terminals[world.b_id].embedding is None


def test_live_baseline_reads_return_the_held_capabilities_and_the_instrument(
    world: _World,
) -> None:
    _name, second = world.baselines[1]

    held = read_baseline_capabilities(second, {world.x_id, world.y_id, "cap_absent"})

    assert set(held) == {world.x_id, world.y_id}
    assert held[world.x_id].name == "Vulnerability patching"
    assert held[world.x_id].embedding is not None
    assert held[world.y_id].embedding is None
    assert read_baseline_instrument_ids(second) == (f"inst_{world.x_id.rsplit('_', 1)[1]}_b",)


def test_live_baseline_requires_read_follows_the_edge_direction_into_the_given_ids(
    world: _World,
) -> None:
    _name, second = world.baselines[1]

    edges = read_baseline_incoming_edges(second, "REQUIRES", {world.x_id})

    assert sorted(edges) == [
        ("obl_direct", world.x_id),
        ("obl_gone", world.x_id),
        ("obl_merged", world.x_id),
    ]
    on_a = read_baseline_incoming_edges(second, "REQUIRES", {world.a_id})
    assert sorted(on_a) == [("obl_on_a", world.a_id)]


def test_live_obligation_redirects_and_existing_sources_are_read(world: _World) -> None:
    assert read_obligation_merge_markers(world.graph) == {"obl_merged": "obl_kept"}
    assert read_existing_sources(
        world.graph, "REQUIRES", {"obl_direct", "obl_kept", "obl_gone"}
    ) == frozenset({"obl_direct", "obl_kept"})


def test_live_default_opener_opens_only_listed_baselines_and_creates_no_graph_key(
    world: _World,
) -> None:
    host, port = falkordb_endpoint()
    db = connect(host=host, port=port, **FALKORDB_TEST_TIMEOUTS)
    keys_before = sorted(db.list_graphs())

    opened = build_default_graph_cleanup_baseline_opener()(world.config)

    names = [name for name, _handle in opened]
    assert names == sorted(names)
    assert {name for name, _handle in world.baselines} <= set(names)
    assert all(name.endswith("_baseline") for name in names)
    assert sorted(db.list_graphs()) == keys_before


def test_live_list_reports_each_resolution_from_the_real_graphs(world: _World) -> None:
    result = list_capability_resolutions(world.graph, world.baselines)

    by_x = {entry.x_id: entry for entry in result.resolutions}
    x_entry = by_x[world.x_id]
    assert (x_entry.x_name, x_entry.a_id) == ("Vulnerability patching", world.a_id)
    assert len(x_entry.instruments) == 2
    assert x_entry.similarity == pytest.approx(0.6)
    assert x_entry.obligation_count == 2
    y_entry = by_x[world.y_id]
    assert (y_entry.a_id, y_entry.marker_resolves_to) == (world.b_id, world.d_id)
    assert y_entry.governing_policy is None
    assert (y_entry.similarity, y_entry.obligation_count) == (None, 0)


def test_live_baseline_covers_and_mitigated_by_reads_follow_the_label_and_direction(
    world: _World,
) -> None:
    _name, second = world.baselines[1]

    covers = read_baseline_incoming_edges(second, "COVERS", {world.x_id, world.a_id})
    mitigated = read_baseline_incoming_edges(second, "MITIGATED_BY", {world.x_id, world.a_id})

    assert sorted(covers) == [("pa_x", world.x_id)]
    assert sorted(mitigated) == [("rp_a", world.a_id)]
    assert read_baseline_incoming_edges(second, "MITIGATED_BY", {world.x_id}) == ()


def test_live_split_preview_classifies_every_edge_type_from_the_real_graphs(
    world: _World,
) -> None:
    preview = preview_capability_split(world.graph, world.baselines, x_id=world.x_id)

    assert (preview.a_id, preview.x_name) == (world.a_id, "Vulnerability patching")
    assert preview.source_baseline == world.baselines[1][0]
    requires = preview.edges.requires
    assert (requires.moves, requires.shared, requires.added, requires.stays) == (
        ("obl_direct",),
        (),
        ("obl_kept",),
        (),
    )
    covers = preview.edges.covers
    assert (covers.moves, covers.stays) == (("pa_x",), ())
    mitigated = preview.edges.mitigated_by
    assert (mitigated.moves, mitigated.stays) == ((), ("rp_a",))
    assert preview.skipped_missing_endpoints == 1
    assert preview.governance is not None
    assert preview.governance.policy.title == "Patch Management Policy"
    assert (
        preview_capability_split(world.graph, world.baselines, x_id=world.x_id).state_digest
        == preview.state_digest
    )
