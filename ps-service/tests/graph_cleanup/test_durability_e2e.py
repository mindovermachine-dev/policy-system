"""A cleanup merge survives live ingest and offline restore (issue #190, slice 17, AC-BI-008).

The real executor runs a real merge (capability and obligation) over the gateway rig (#212). The
persisted effect is then read back from the rig graph the gateway group built (a `merged`
tombstone with `MERGED_INTO`; a `MergedObligation {merged_into}` marker), and served as the
single-tenant graph to the real Company Merge live path (`dedupe_canonical_nodes`,
`resolve_obligation_redirects`) and to the real offline restore merge. No FalkorDB runs here:
the real redirect readers (`read_capability_tombstone`, `read_obligation_marker`) are run over
the rig graph to pin the shapes the live path looks for, and the `falkordb_live` tests
(unverified without FalkorDB) pin the frozen legacy statements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from authz._fakes import FakeAccessRoleStore

import ps_service.restore.restore_instrument as restore_instrument_module
from graph_cleanup._fakes import (
    ABSORBED,
    OBL_ABSORBED,
    OBL_SURVIVOR,
    SURVIVOR,
)
from ps_service.authz.models import AccessRole
from ps_service.company_merge.dedup import dedupe_canonical_nodes
from ps_service.company_merge.models import BaselineGraph, BaselineNode
from ps_service.company_merge.obligation_redirect import resolve_obligation_redirects
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.executors import (
    TOOL_MERGE_CAPABILITIES,
    TOOL_MERGE_OBLIGATIONS,
    execute_capability_merge,
    execute_obligation_merge,
)
from ps_service.graph_cleanup.graph_reader import (
    read_capability_merge_state,
    read_capability_tombstone,
    read_obligation_marker,
    read_obligation_merge_state,
)
from ps_service.graph_cleanup.merge_planner import plan_capability_merge
from ps_service.graph_cleanup.obligation_planner import plan_obligation_merge
from ps_service.logging import configure
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from falkordb import FalkorDB

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


class _Node:
    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


@dataclass
class _Persisted:
    """What the single-tenant graph holds after the merge, read back from the rig graph."""

    tombstones: dict[str, str] = field(default_factory=dict)
    markers: dict[str, str] = field(default_factory=dict)
    survivors: set[str] = field(default_factory=set)
    calls: list[tuple[str, dict[str, object] | None]] = field(default_factory=list)

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        self.calls.append((q, params))
        if "MergedObligation" in q:
            return _Result([[k, v] for k, v in self.markers.items()])
        if "MATCH (o:Obligation) WHERE o.id IN $ids" in q:
            ids = cast("list[str]", (params or {})["ids"])
            return _Result([[i] for i in ids if i in self.survivors])
        if "MERGED_INTO" in q and "{status: 'merged'}" in q:
            return _Result([[k, v] for k, v in self.tombstones.items()])
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            rows: list[object] = [[s, "Survivor", [0.0, 1.0]] for s in sorted(self.survivors)]
            rows += [[t, "Absorbed", [1.0, 0.0]] for t in self.tombstones]
            return _Result(rows)
        if q == "UNWIND $ids AS id MATCH (n {id: id}) RETURN id":
            return _Result([])
        return _Result([[0]])

    def writes(self, fragment: str) -> list[dict[str, object] | None]:
        return [p for q, p in self.calls if fragment in q]


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
    redirects = {k[2]: k[4] for k in rig.graph.edges if k[0] == "MERGED_INTO"}
    return _Persisted(tombstones=redirects, survivors=set(redirects.values()))


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

    assert read_capability_tombstone(cleanup_rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED)


def test_the_obligation_merge_leaves_exactly_what_the_redirect_reader_looks_for(
    cleanup_rig: CleanupRig,
) -> None:
    _merge_obligations(cleanup_rig)

    assert read_obligation_marker(
        cleanup_rig.read, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED
    )


def test_live_ingest_after_a_capability_merge_resolves_to_the_survivor_and_mints_nothing(
    cleanup_rig: CleanupRig,
) -> None:
    persisted = _merge_capabilities(cleanup_rig)

    result = dedupe_canonical_nodes(
        (BaselineNode(id=ABSORBED, properties={"name": "Absorbed", "confidence": 0.9}),),
        kind="Capability",
        single_tenant_graph=cast("GraphHandle", persisted),
        model="fake-embed-model",
        threshold=_THRESHOLD,
    )

    assert [(r.incoming_id, r.canonical_id, r.match_kind) for r in result.resolutions] == [
        (ABSORBED, SURVIVOR, "redirected")
    ]
    assert result.near_misses == ()


class _CapabilityBaseline:
    """The baseline artifact regenerates the absorbed Capability, required by one Obligation."""

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del params
        if "[:REQUIRES]" in q:
            return _Result([[_OBLIGATION, ABSORBED]])
        if "(n:PracticeArea)" in q or "(n:RiskPath)" in q:
            return _Result([])
        if "n.description" in q:
            return _Result([[ABSORBED, "Absorbed", 0.8, None, None]])
        if "(n:Obligation) RETURN" in q:
            return _Result([[_OBLIGATION, "Do the thing.", 0.9]])
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _Result([[_Node({"id": _INSTRUMENT_ID, "title": "R"})]])
        return _Result([])


class _ObligationBaseline:
    """The baseline artifact regenerates the absorbed Obligation under its Role."""

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del params
        if "[:HAS]" in q:
            return _Result([[_ROLE, OBL_ABSORBED]])
        if "[:SATISFIED_BY]" in q:
            return _Result([[_REQUIREMENT, OBL_ABSORBED]])
        if "[:REQUIRES]" in q:
            return _Result([[OBL_ABSORBED, "cap_1"]])
        if any(f"(n:{label})" in q for label in ("PracticeArea", "RiskPath")):
            return _Result([])
        if "n.description" in q:
            return _Result([["cap_1", "Thing Doing", 0.8, None, None]])
        if "(n:Obligation) RETURN" in q:
            return _Result([[OBL_ABSORBED, "Report  incidents.", 0.9]])
        if "n.role_id" in q:
            return _Result([[_REQUIREMENT, "Must do.", "requirement", 0.9, _ROLE]])
        if "n.name, n.confidence" in q:
            return _Result([[_ROLE, "Operator", 0.9]])
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _Result([[_Node({"id": _INSTRUMENT_ID, "title": "R"})]])
        return _Result([])


class _Db:
    def __init__(self, graphs: dict[str, object]) -> None:
        self._graphs = graphs

    def select_graph(self, name: str) -> object:
        return self._graphs[name]


def _restore(
    baseline: object, persisted: _Persisted, embeddings: dict[str, tuple[float, ...]]
) -> None:
    db = cast(
        "FalkorDB",
        _Db({"baseline__restoring__t": baseline, "policy_system__restoring__t": persisted}),
    )
    restore_instrument_module._run_baseline_merge(  # pyright: ignore[reportPrivateUsage]
        db,
        "baseline__restoring__t",
        _INSTRUMENT_ID,
        embeddings,
        _THRESHOLD,
        "policy_system__restoring__t",
        None,
    )


def test_offline_restore_after_a_capability_merge_attaches_to_the_survivor_and_mints_nothing(
    cleanup_rig: CleanupRig,
) -> None:
    persisted = _merge_capabilities(cleanup_rig)

    _restore(_CapabilityBaseline(), persisted, {ABSORBED: (1.0, 0.0)})

    assert persisted.writes("MERGE (n:Capability {id: $id}) ON CREATE SET") == []
    assert persisted.writes("[:REQUIRES]") == [{"source_id": _OBLIGATION, "target_id": SURVIVOR}]


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

    rewritten, mapping = resolve_obligation_redirects(
        cast("GraphHandle", persisted), _obligation_baseline_graph()
    )

    assert mapping == {OBL_ABSORBED: OBL_SURVIVOR}
    assert rewritten.obligation_nodes == ()


def test_offline_restore_after_an_obligation_merge_re_targets_onto_the_survivor(
    cleanup_rig: CleanupRig,
) -> None:
    persisted = _merge_obligations(cleanup_rig)

    _restore(_ObligationBaseline(), persisted, {})

    assert persisted.writes("MERGE (n:Obligation {id: $id}) ON CREATE SET") == []
    assert persisted.writes("[:SATISFIED_BY]") == [
        {"source_id": _REQUIREMENT, "target_id": OBL_SURVIVOR}
    ]
    assert persisted.writes("[:REQUIRES]") == [{"source_id": OBL_SURVIVOR, "target_id": "cap_1"}]
    assert persisted.writes("[:HAS]") == [{"source_id": _ROLE, "target_id": OBL_SURVIVOR}]
