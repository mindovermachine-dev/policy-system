"""The migrated internal-seed writer builds the same graphs the pre-migration writer did.

The reference is `_legacy_reference.write_legacy`, a frozen copy of the old writer's Cypher run
against a recording graph. Two independent checks, per graph (native and baseline):

* Structural cross-check (independent of the gateway fake): the multiset of node writes
  (label, id, properties) and edge writes (type, endpoints, properties) read from the legacy
  call parameters equals the multiset of primitives in the group the new writer submitted.
* Digest equivalence modulo identity: `ps-graph-digest-v1` of the graph the legacy calls
  build, with every edge stamped with the gateway's deterministic identity, equals the digest of
  the graph the gateway built. Anything else that differs still fails the digest.

Met modulo identity; the live digest is not verified here (no FalkorDB/Postgres in this sandbox).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ingestion.adapters.internal_seed._legacy_reference import (
    LegacyRecordingGraph,
    primitive_edge_facts,
    primitive_node_facts,
    write_legacy,
)
from ingestion.adapters.internal_seed._seeds import Run, full_seed
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.ingestion.adapters.internal_seed.adapter import InternalSeedIngestionAdapter

if TYPE_CHECKING:
    from collections.abc import Callable

    from graph_gateway._fakes import GatewayRig

    from ps_service.ingestion.adapters.internal_seed.models import InternalRegulationSeed

_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
_AUTHORED = json.loads(
    (_FIXTURES / "authored-governance" / "authored-governance-seed.json").read_text("utf-8")
)


def _authored_governance_seed() -> InternalRegulationSeed:
    return InternalSeedIngestionAdapter().parse_seed(_AUTHORED)


_SEEDS = [
    pytest.param(full_seed, id="every-label"),
    pytest.param(_authored_governance_seed, id="authored-governance-fixture"),
]


def _group(run: Run, graph: str):
    (group,) = (g for g in run.submitter.groups if g.graph == graph)
    return group


@pytest.mark.parametrize("make_seed", _SEEDS)
def test_node_and_edge_writes_match_the_legacy_calls_one_for_one(
    rig: GatewayRig, make_seed: Callable[[], InternalRegulationSeed]
) -> None:
    seed = make_seed()
    legacy_native, legacy_baseline = LegacyRecordingGraph(), LegacyRecordingGraph()
    write_legacy(seed, legacy_native, legacy_baseline)
    run = Run(rig)

    run.ingest(seed)

    for legacy, graph in ((legacy_native, run.native_name), (legacy_baseline, run.baseline_name)):
        group = _group(run, graph)
        assert primitive_node_facts(group) == legacy.node_facts()
        assert primitive_edge_facts(group) == legacy.edge_facts()
        assert sum(primitive_node_facts(group).values()) > 0


@pytest.mark.parametrize("make_seed", _SEEDS)
def test_native_and_baseline_digests_equal_the_legacy_graphs_modulo_edge_identity(
    rig: GatewayRig, make_seed: Callable[[], InternalRegulationSeed]
) -> None:
    seed = make_seed()
    legacy_native, legacy_baseline = LegacyRecordingGraph(), LegacyRecordingGraph()
    write_legacy(seed, legacy_native, legacy_baseline)
    run = Run(rig)

    run.ingest(seed)

    assert canonical_digest(run.native) == canonical_digest(legacy_native.as_gateway_graph())
    assert canonical_digest(run.baseline) == canonical_digest(legacy_baseline.as_gateway_graph())


def test_the_digest_comparison_fails_when_one_property_differs(rig: GatewayRig) -> None:
    """Guard of the guard: a one-property difference in the new graph breaks the equality."""
    seed = full_seed()
    legacy_native, legacy_baseline = LegacyRecordingGraph(), LegacyRecordingGraph()
    write_legacy(seed, legacy_native, legacy_baseline)
    run = Run(rig)
    run.ingest(seed)
    run.native.nodes[("Capability", "cap-1")]["name"] = "tampered"

    assert canonical_digest(run.native) != canonical_digest(legacy_native.as_gateway_graph())
    assert canonical_digest(run.baseline) == canonical_digest(legacy_baseline.as_gateway_graph())


def test_the_digest_comparison_fails_when_an_edge_is_missing(rig: GatewayRig) -> None:
    seed = full_seed()
    legacy_native, legacy_baseline = LegacyRecordingGraph(), LegacyRecordingGraph()
    write_legacy(seed, legacy_native, legacy_baseline)
    run = Run(rig)
    run.ingest(seed)
    del run.baseline.edges[next(iter(run.baseline.edges))]

    assert canonical_digest(run.baseline) != canonical_digest(legacy_baseline.as_gateway_graph())
