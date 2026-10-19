"""A cleanup merge survives live ingest and offline restore.

The real executor runs a real merge (capability and obligation) over the gateway rig. The
persisted effect is then read back from the rig graph the gateway group built (a
`MergedCapability {merged_into}` or `MergedObligation {merged_into}` marker, the absorbed node
deleted), and served as the single-tenant graph to the real Company Merge live path
(`resolve_redirects`) and to the real offline restore merge. No FalkorDB runs here:
the real marker readers (`read_capability_merged`, `read_obligation_marker`) are run over
the rig graph to pin the shapes the live path looks for, and the `falkordb_live` tests
(unverified without FalkorDB) pin the frozen legacy statements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from authz._fakes import FakeAccessRoleStore

from graph_cleanup._fakes import (
    ABSORBED,
    OBL_ABSORBED,
    OBL_SURVIVOR,
    SURVIVOR,
)
from ps_service.authz.models import AccessRole
from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.company_merge.models import BaselineGraph, BaselineNode
from ps_service.company_merge.redirects import resolve_redirects
from ps_service.config import ServiceConfig
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.graph_cleanup.executors import (
    TOOL_MERGE_CAPABILITIES,
    TOOL_MERGE_OBLIGATIONS,
    execute_capability_merge,
    execute_obligation_merge,
)
from ps_service.graph_cleanup.graph_reader import (
    read_capability_merge_state,
    read_capability_merged,
    read_obligation_marker,
    read_obligation_merge_state,
)
from ps_service.graph_cleanup.merge_planner import plan_capability_merge
from ps_service.graph_cleanup.obligation_planner import plan_obligation_merge
from ps_service.graph_gateway.models import UpsertEdge, UpsertNode
from ps_service.logging import configure
from ps_service.passkey_signing.models import PendingApprovalRow
from ps_service.restore.merge_primitives import PolicySystemLeg, build_policy_system_leg

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies

_OWNER = ("owner", "https://issuer.example.com/")
_OFFICER = ("officer", "https://issuer.example.com/")
_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=False,
)
_THRESHOLD = 0.85
_INSTRUMENT_ID = "REG-R-1.0"
_ROLE = "role_r"
_REQUIREMENT = "REG-R-1.0_req_art_1.1"
_OBLIGATION = "obl_new"


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


class _Result:
    def __init__(self, result_set: list[object]) -> None:
        self.result_set = result_set


@dataclass
class _Persisted:
    """What the single-tenant graph holds after the merge, read back from the rig graph."""

    capability_markers: dict[str, str] = field(default_factory=dict)
    markers: dict[str, str] = field(default_factory=dict)
    survivors: set[str] = field(default_factory=set)
    calls: list[tuple[str, dict[str, object] | None]] = field(default_factory=list)

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        self.calls.append((q, params))
        if "MergedCapability" in q:
            return _Result([[k, v] for k, v in self.capability_markers.items()])
        if "MergedObligation" in q:
            return _Result([[k, v] for k, v in self.markers.items()])
        if "MATCH (o:Obligation) WHERE o.id IN $ids" in q:
            ids = cast("list[str]", (params or {})["ids"])
            return _Result([[i] for i in ids if i in self.survivors])
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            rows: list[object] = [[s, "Survivor", [0.0, 1.0]] for s in sorted(self.survivors)]
            return _Result(rows)
        if "WHERE n.id IN $ids RETURN n.id" in q:  # the restore's batched existence reads
            ids = cast("list[str]", (params or {})["ids"])
            return _Result([[i] for i in ids if i in self.survivors])
        return _Result([])


def _roles() -> FakeAccessRoleStore:
    store = FakeAccessRoleStore(expected_owner=_OWNER)
    store.bootstrap_first_owner(_OWNER)
    store.grant(actor=_OWNER, target=_OFFICER, access_role=AccessRole.COMPLIANCE_OFFICER)
    return store


def _row(tool: str, args: dict[str, object]) -> PendingApprovalRow:
    now = datetime.now(UTC)
    return PendingApprovalRow(
        id="approval-1",
        code_hash=b"h",
        tool_name=tool,
        normalized_args=args,
        actor_subject=_OFFICER[0],
        actor_issuer=_OFFICER[1],
        nonce=b"n",
        display_summary={},
        status="signed",
        outcome=None,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )


def _deps(rig: CleanupRig) -> GraphCleanupDependencies:
    return rig.dependencies(_roles())


def _merge_capabilities(rig: CleanupRig) -> _Persisted:
    rig.seed_capability_merge_world()
    digest = plan_capability_merge(
        read_capability_merge_state(rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED)
    ).state_digest
    row = _row(
        TOOL_MERGE_CAPABILITIES,
        {
            "survivor_id": SURVIVOR,
            "absorbed_id": ABSORBED,
            "acknowledge_governance_change": False,
            "state_digest": digest,
        },
    )
    outcome = execute_capability_merge(row, _CONFIG, _deps(rig))
    assert outcome["merged"] is True
    assert len(rig.cleanup_groups()) == 1
    markers = {
        node_id: cast("str", props["merged_into"])
        for (label, node_id), props in rig.graph.nodes.items()
        if label == "MergedCapability"
    }
    return _Persisted(capability_markers=markers, survivors=set(markers.values()))


def _merge_obligations(rig: CleanupRig) -> _Persisted:
    rig.seed_obligation_merge_world()
    digest = plan_obligation_merge(
        read_obligation_merge_state(rig.read, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED)
    ).state_digest
    row = _row(
        TOOL_MERGE_OBLIGATIONS,
        {"survivor_id": OBL_SURVIVOR, "absorbed_id": OBL_ABSORBED, "state_digest": digest},
    )
    outcome = execute_obligation_merge(row, _CONFIG, _deps(rig))
    assert outcome["merged"] is True
    assert len(rig.cleanup_groups()) == 1
    markers = {
        node_id: cast("str", props["merged_into"])
        for (label, node_id), props in rig.graph.nodes.items()
        if label == "MergedObligation"
    }
    return _Persisted(markers=markers, survivors=set(markers.values()))


def test_the_capability_merge_leaves_exactly_what_the_redirect_reader_looks_for(
    cleanup_rig: CleanupRig,
) -> None:
    _merge_capabilities(cleanup_rig)

    assert read_capability_merged(cleanup_rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED)


def test_the_obligation_merge_leaves_exactly_what_the_redirect_reader_looks_for(
    cleanup_rig: CleanupRig,
) -> None:
    _merge_obligations(cleanup_rig)

    assert read_obligation_marker(
        cleanup_rig.read, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED
    )


def _capability_baseline_graph() -> BaselineGraph:
    return BaselineGraph(
        regulatory_instrument_id=_INSTRUMENT_ID,
        regulatory_instrument_properties={"id": _INSTRUMENT_ID},
        role_nodes=(),
        requirement_nodes=(),
        obligation_nodes=(),
        capability_nodes=(
            BaselineNode(id=ABSORBED, properties={"name": "Absorbed", "confidence": 0.9}),
        ),
        provenance_edges=(),
        bare_edges=(),
    )


def test_live_ingest_after_a_capability_merge_resolves_to_the_survivor_and_mints_nothing(
    cleanup_rig: CleanupRig,
) -> None:
    persisted = _merge_capabilities(cleanup_rig)

    resolution = resolve_redirects(cast("GraphHandle", persisted), _capability_baseline_graph())

    assert resolution.mapping == {ABSORBED: SURVIVOR}
    assert resolution.baseline.capability_nodes == ()


def _edge(kind: str, source: tuple[str, str], target: tuple[str, str]) -> SerializedEdge:
    return SerializedEdge(kind, source[0], source[1], target[0], target[1], {})


def _capability_baseline(embedding: list[float]) -> SerializedGraph:
    """The baseline artifact regenerates the absorbed Capability, required by one Obligation."""
    return SerializedGraph(
        nodes=(
            SerializedNode("RegulatoryInstrument", {"id": _INSTRUMENT_ID, "title": "R"}),
            SerializedNode(
                "Obligation", {"id": _OBLIGATION, "text": "Do the thing.", "confidence": 0.9}
            ),
            SerializedNode(
                "Capability",
                {"id": ABSORBED, "name": "Absorbed", "confidence": 0.8, "embedding": embedding},
            ),
        ),
        edges=(_edge("REQUIRES", ("Obligation", _OBLIGATION), ("Capability", ABSORBED)),),
    )


def _obligation_baseline() -> SerializedGraph:
    """The baseline artifact regenerates the absorbed Obligation under its Role."""
    return SerializedGraph(
        nodes=(
            SerializedNode("RegulatoryInstrument", {"id": _INSTRUMENT_ID, "title": "R"}),
            SerializedNode("Role", {"id": _ROLE, "name": "Operator", "confidence": 0.9}),
            SerializedNode(
                "Requirement",
                {
                    "id": _REQUIREMENT,
                    "text": "Must do.",
                    "type": "requirement",
                    "confidence": 0.9,
                    "role_id": _ROLE,
                },
            ),
            SerializedNode(
                "Obligation", {"id": OBL_ABSORBED, "text": "Report  incidents.", "confidence": 0.9}
            ),
            SerializedNode("Capability", {"id": "cap_1", "name": "Thing Doing", "confidence": 0.8}),
        ),
        edges=(
            _edge("HAS", ("Role", _ROLE), ("Obligation", OBL_ABSORBED)),
            _edge("SATISFIED_BY", ("Requirement", _REQUIREMENT), ("Obligation", OBL_ABSORBED)),
            _edge("REQUIRES", ("Obligation", OBL_ABSORBED), ("Capability", "cap_1")),
        ),
    )


def _restore(baseline: SerializedGraph, persisted: _Persisted) -> PolicySystemLeg:
    """The `policy_system` leg of a restore of `baseline`, built over what the merge persisted."""
    return build_policy_system_leg(
        baseline,
        instrument_id=_INSTRUMENT_ID,
        live=cast("GraphHandle", persisted),
        governance=GovernanceImport(),
        similarity_threshold=_THRESHOLD,
        emitter=None,
    )


def _nodes(leg: PolicySystemLeg, label: str) -> list[UpsertNode]:
    return [p for p in leg.primitives if isinstance(p, UpsertNode) and p.label == label]


def _edges(leg: PolicySystemLeg, edge_type: str) -> list[tuple[str, str]]:
    return [
        (p.source.id, p.target.id)
        for p in leg.primitives
        if isinstance(p, UpsertEdge) and p.type == edge_type
    ]


def test_offline_restore_after_a_capability_merge_attaches_to_the_survivor_and_mints_nothing(
    cleanup_rig: CleanupRig,
) -> None:
    persisted = _merge_capabilities(cleanup_rig)

    leg = _restore(_capability_baseline([1.0, 0.0]), persisted)

    assert _nodes(leg, "Capability") == []
    assert _edges(leg, "REQUIRES") == [(_OBLIGATION, SURVIVOR)]


def _obligation_baseline_graph() -> BaselineGraph:
    return BaselineGraph(
        regulatory_instrument_id=_INSTRUMENT_ID,
        regulatory_instrument_properties={"id": _INSTRUMENT_ID},
        role_nodes=(),
        requirement_nodes=(),
        obligation_nodes=(
            BaselineNode(
                id=OBL_ABSORBED, properties={"text": "Report  incidents.", "confidence": 0.9}
            ),
        ),
        capability_nodes=(),
        provenance_edges=(),
        bare_edges=(),
    )


def test_live_ingest_after_an_obligation_merge_drops_the_absorbed_obligation(
    cleanup_rig: CleanupRig,
) -> None:
    persisted = _merge_obligations(cleanup_rig)

    resolution = resolve_redirects(cast("GraphHandle", persisted), _obligation_baseline_graph())

    assert resolution.mapping == {OBL_ABSORBED: OBL_SURVIVOR}
    assert resolution.baseline.obligation_nodes == ()


def test_offline_restore_after_an_obligation_merge_re_targets_onto_the_survivor(
    cleanup_rig: CleanupRig,
) -> None:
    persisted = _merge_obligations(cleanup_rig)

    leg = _restore(_obligation_baseline(), persisted)

    assert _nodes(leg, "Obligation") == []
    assert _edges(leg, "SATISFIED_BY") == [(_REQUIREMENT, OBL_SURVIVOR)]
    assert _edges(leg, "REQUIRES") == [(OBL_SURVIVOR, "cap_1")]
    assert _edges(leg, "HAS") == [(_ROLE, OBL_SURVIVOR)]
