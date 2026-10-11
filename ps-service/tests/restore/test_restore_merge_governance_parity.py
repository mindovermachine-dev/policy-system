"""Restore and live merge import a policy tree through one function (issue #227, AC-BI-005).

The real engineering-practices seed is ingested into a gateway rig's baseline graph, then
imported once by `merge_baseline_graph` (live internal ingest) and once by restore's
`_run_baseline_merge`, each into its own empty `policy_system` stand-in. The Policy/Standard/
Control nodes and the governance and classification edges must be identical.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
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


def _comparable(store: SingleTenantStore) -> tuple[object, object]:
    """The governance nodes (without their creation timestamp) and governance edges of `store`."""
    nodes = {
        (label, node_id): {k: v for k, v in props.items() if k != "created_at"}
        for label in _GOVERNANCE_LABELS
        for node_id, props in store.nodes_of(label).items()
    }
    edges = {edge for edge in store.edges if edge[0] in _GOVERNANCE_EDGES}
    return nodes, edges


def test_restore_and_live_merge_write_identical_governance_trees(
    rig: GatewayRig, make_emitter: MakeEmitter
) -> None:
    emitter, _log_path = make_emitter()
    seed = parse_seed(json.loads(_SEED_PATH.read_text(encoding="utf-8")))
    baseline_name = ingest_seed(rig, seed)
    baseline = BaselineGraphReader(rig.graphs.open(baseline_name))

    merged = SingleTenantStore()
    merge_baseline_graph(
        _RID,
        baseline_graph=baseline,
        single_tenant_graph=merged,
        embed_model=EMBED_MODEL,
        similarity_threshold=SIMILARITY_THRESHOLD,
        call_embedding=OneHotEmbeddingCaller(),
        emitter=emitter,
        governance=GovernanceImport(owner=_OWNER),
    )

    restored = SingleTenantStore()
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

    merged_nodes, merged_edges = _comparable(merged)
    restored_nodes, restored_edges = _comparable(restored)
    assert len(cast("dict[object, object]", merged_nodes)) == 11 + 21 + 39
    assert merged_nodes == restored_nodes
    assert merged_edges == restored_edges
