"""Restore and live merge import a policy tree through one function (issue #227, AC-BI-005).

The real engineering-practices seed is ingested into a gateway rig's baseline graph, then
imported once by `merge_baseline_graph` (live internal ingest) and once by restore's
`_run_baseline_merge`, each into its own empty `policy_system` stand-in. The Policy/Standard/
Control nodes and the governance and classification edges must be identical.

Both paths build the tree with the same shared builders and submit it to the gateway (live merge
as one group on `policy_system`, restore as the `policy_system` leg of its multi-graph group). The
restore side is read here as the primitives of its leg (`build_policy_system_leg`), compared with
the tree the live merge logged: the parity this test pins is the RESULT.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from company_merge._gateway_fakes import MergeRig
from company_merge._seed_merge_graphs import (
    EMBED_MODEL,
    SIMILARITY_THRESHOLD,
    BaselineGraphReader,
    OneHotEmbeddingCaller,
    SingleTenantStore,
    ingest_seed,
    parse_seed,
)
from graph_gateway._fakes import GatewayRig

from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.graph_gateway.models import UpsertEdge, UpsertNode
from ps_service.restore.merge_primitives import build_policy_system_leg

if TYPE_CHECKING:
    from collections.abc import Iterator

    from company_merge._fakes import MakeEmitter
    from graph_gateway._fakes import InMemoryGraph

    from ps_service.export.models import SerializedPropertyValue

_SEED_PATH = (
    Path(__file__).resolve().parents[3]
    / "internal-sources"
    / "engineering-practices"
    / "engineering-practices-seed.json"
)
_OWNER = ("sub-owner-1", "https://idp.example")
_RID = "ENGPRAC-1.0"
_GOVERNANCE_LABELS = ("Policy", "Standard", "Control")
_GOVERNANCE_EDGES = frozenset(
    {"GOVERNED_BY", "SUPPORTED_BY", "IMPLEMENTED_BY", "OWNS", "VERIFIED_BY"}
)


def _as_artifact_baseline(graph: InMemoryGraph) -> SerializedGraph:
    """The rig's baseline graph as the parsed artifact an export of it would hold."""
    return SerializedGraph(
        nodes=tuple(
            SerializedNode(
                label, cast("dict[str, SerializedPropertyValue]", {"id": node_id, **properties})
            )
            for (label, node_id), properties in graph.nodes.items()
        ),
        edges=tuple(
            SerializedEdge(
                edge_type,
                source_label,
                source_id,
                target_label,
                target_id,
                cast(
                    "dict[str, SerializedPropertyValue]",
                    {k: v for k, v in properties.items() if k != "identity"},
                ),
            )
            for (edge_type, source_label, source_id, target_label, target_id, _), properties in (
                graph.edges.items()
            )
        ),
    )


@pytest.fixture
def rig() -> Iterator[GatewayRig]:
    gateway_rig = GatewayRig()
    yield gateway_rig
    gateway_rig.close()


@pytest.fixture
def merge_rig() -> Iterator[MergeRig]:
    rig = MergeRig()
    yield rig
    rig.close()


def _comparable(store: SingleTenantStore) -> tuple[object, object]:
    """The governance nodes (without their creation timestamp) and governance edges of `store`."""
    nodes = {
        (label, node_id): {k: v for k, v in props.items() if k != "created_at"}
        for label in _GOVERNANCE_LABELS
        for node_id, props in store.nodes_of(label).items()
    }
    edges = {edge for edge in store.edges if edge[0] in _GOVERNANCE_EDGES}
    return nodes, edges


def _gateway_governance_edges(rig: MergeRig) -> set[tuple[str, str, str]]:
    """The governance edges the gateway wrote, read from the rig's graph (issue #210).

    The merge side writes edges as gateway primitives, so they carry the gateway's `identity`
    property (`TYPE:source->target`). Nothing else may differ: the identity is checked against the
    endpoints and then dropped from the comparison.
    """
    edges: set[tuple[str, str, str]] = set()
    for key, props in rig.policy_system.edges.items():
        edge_type, _source_label, source_id, _target_label, target_id, identity = key
        if edge_type not in _GOVERNANCE_EDGES:
            continue
        assert identity == f"{edge_type}:{source_id}->{target_id}"
        assert props == {"identity": identity}
        edges.add((edge_type, source_id, target_id))
    return edges


def test_restore_and_live_merge_write_identical_governance_trees(
    rig: GatewayRig, make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    emitter, _log_path = make_emitter()
    seed = parse_seed(json.loads(_SEED_PATH.read_text(encoding="utf-8")))
    baseline_name = ingest_seed(rig, seed)
    baseline = BaselineGraphReader(rig.graphs.open(baseline_name))

    merged = merge_rig.legacy
    merge_baseline_graph(
        _RID,
        baseline_graph=baseline,
        single_tenant_graph=merged,
        embed_model=EMBED_MODEL,
        similarity_threshold=SIMILARITY_THRESHOLD,
        call_embedding=OneHotEmbeddingCaller(),
        emitter=emitter,
        governance=GovernanceImport(owner=_OWNER),
        **merge_rig.merge_keywords(),
    )

    restored = build_policy_system_leg(
        _as_artifact_baseline(rig.graphs.open(baseline_name)),
        instrument_id=_RID,
        live=SingleTenantStore(),
        governance=GovernanceImport(owner=_OWNER),
        similarity_threshold=SIMILARITY_THRESHOLD,
        emitter=emitter,
    )

    gateway_governance = [entry for entry in merge_rig.logged() if entry.name in _GOVERNANCE_LABELS]
    assert len(gateway_governance) == 11 + 21 + 39
    assert len({entry.group_id for entry in gateway_governance}) == 1
    restored_nodes = {
        (p.label, p.id): {k: v for k, v in p.properties.items() if k != "created_at"}
        for p in restored.primitives
        if isinstance(p, UpsertNode) and p.label in _GOVERNANCE_LABELS
    }
    assert sum(1 for label, _id in restored_nodes if label == "Policy") == 11
    for primitive in restored.primitives:
        if isinstance(primitive, UpsertNode) and primitive.label in _GOVERNANCE_LABELS:
            # only a Policy is stamped, and the stamp is an ISO-8601 instant on either path
            assert ("created_at" in primitive.properties) == (primitive.label == "Policy")
            if primitive.label == "Policy":
                assert datetime.fromisoformat(str(primitive.properties["created_at"]))
    for label in _GOVERNANCE_LABELS:
        for props in merged.nodes_of(label).values():
            assert ("created_at" in props) == (label == "Policy")

    merged_nodes, _view = _comparable(merged)
    merged_edges = _gateway_governance_edges(merge_rig)
    restored_edges = {
        (p.type, p.source.id, p.target.id)
        for p in restored.primitives
        if isinstance(p, UpsertEdge) and p.type in _GOVERNANCE_EDGES
    }
    assert len(cast("dict[object, object]", merged_nodes)) == 11 + 21 + 39
    assert merged_nodes == restored_nodes
    assert merged_edges
    assert merged_edges == restored_edges
