"""Restore and live merge import a policy tree through one function (issue #227, AC-BI-005).

The real engineering-practices seed is ingested into a gateway rig's baseline graph, then
imported once by `merge_baseline_graph` (live internal ingest) and once by restore's
`_run_baseline_merge`, each into its own empty `policy_system` stand-in. The Policy/Standard/
Control nodes and the governance and classification edges must be identical.

Since issue #210 the two paths write by different mechanisms (D1): live merge submits the tree as
one Graph Write Gateway group, restore writes it with the verbatim snapshot Cypher into its
staged copy. The parity this test pins is the RESULT; it also asserts the mechanisms really
differ, so the equality cannot pass by both going through the same writer.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from company_merge._gateway_fakes import MergeRig
from company_merge._legacy_reference import SnapshotWriteStore
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

import ps_service.restore.restore_instrument as restore_instrument_module
from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.company_merge.merge import merge_baseline_graph

if TYPE_CHECKING:
    from collections.abc import Iterator

    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB

    from ps_service.company_merge.falkordb_client import GraphHandle

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


class _FakeDb:
    """A name -> `GraphHandle` lookup for `_run_baseline_merge`'s `select_graph` calls."""

    def __init__(self, graphs: dict[str, GraphHandle]) -> None:
        self._graphs = graphs

    def select_graph(self, name: str) -> GraphHandle:
        return self._graphs[name]


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
    property (`TYPE:source->target`); the Restore snapshot edges are bare. Nothing else may differ:
    the identity is checked against the endpoints and then dropped from the comparison.
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

    restored = SnapshotWriteStore()
    fake_db = cast("FalkorDB", _FakeDb({baseline_name: baseline, "snapshot": restored}))
    restore_instrument_module._run_baseline_merge(  # pyright: ignore[reportPrivateUsage]
        fake_db,
        baseline_name,
        _RID,
        {},
        SIMILARITY_THRESHOLD,
        "snapshot",
        emitter,
        GovernanceImport(owner=_OWNER),
    )

    gateway_governance = [entry for entry in merge_rig.logged() if entry.name in _GOVERNANCE_LABELS]
    assert len(gateway_governance) == 11 + 21 + 39
    assert len({entry.group_id for entry in gateway_governance}) == 1
    assert sum(1 for s in restored.statements if "ON CREATE SET" in s and "Policy" in s) == 11
    for store in (merged, restored):
        for label in _GOVERNANCE_LABELS:
            for props in store.nodes_of(label).values():
                # only a Policy is stamped, and the stamp is an ISO-8601 instant on either path
                assert ("created_at" in props) == (label == "Policy")
                if label == "Policy":
                    assert datetime.fromisoformat(str(props["created_at"]))

    merged_nodes, _view = _comparable(merged)
    merged_edges = _gateway_governance_edges(merge_rig)
    restored_nodes, restored_edges = _comparable(restored)
    assert len(cast("dict[object, object]", merged_nodes)) == 11 + 21 + 39
    assert merged_nodes == restored_nodes
    assert merged_edges
    assert merged_edges == restored_edges
