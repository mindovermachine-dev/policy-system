"""`merge_baseline_graph` writes PracticeArea/RiskPath through the Graph Write Gateway (issue #210).

Slice 4b: PracticeArea and RiskPath are exact-identity create-only passthrough nodes (issue #106).
Both labels leave Company Merge as ONE gateway group; a node the graph already holds is left out
of it (decided BEFORE submission from one batched read per label), so it keeps every property and
nothing is logged for it (AC-BI-005). A baseline without classification-layer content reads and
submits nothing (issue #106 AC-BI-009). The pre-write endpoint validation is a read through the
read wrapper.

Equivalence (AC-BI-002, S-classification) against the frozen legacy reference is met modulo edge
identity; live digest and live interpreter agreement are not verifiable here (no FalkorDB).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from company_merge._gateway_fakes import (
    POLICY_SYSTEM,
    FamilyABaseline,
    MergeRig,
    family_a_baseline_graph,
)
from company_merge._legacy_reference import (
    FROZEN_STATEMENT_HASHES,
    LegacyRecordingGraph,
    write_legacy_instrument_roles_requirements,
    write_legacy_practice_areas_and_risk_paths,
)
from company_merge._seed_merge_graphs import EMBED_MODEL, SIMILARITY_THRESHOLD, BaselineGraphReader
from ps_service.company_merge import graph_reader, graph_writer
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.models import BareEdge, BaselineNode
from ps_service.dependency_health import FALKORDB, is_healthy
from ps_service.graph_gateway.digest import canonical_digest

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from ps_service.company_merge.falkordb_client import GraphQueryResult
    from ps_service.logging import LogEmitter

_PA = "pa_secure_sdlc_4a7c1d"
_RP = "rp_secure_build_release_d93f8a"
_PA_PROPS: dict[str, str | float] = {
    "name": "Secure SDLC",
    "status": "active",
    "description": "Secure development lifecycle practices",
    "version": "1.0",
    "owner_id": "role_ciso",
}
_RP_PROPS: dict[str, str | float] = {
    "name": "Secure Build & Release",
    "status": "active",
    "risk_type": "operational",
    "version": "2.0",
}
_FRESH = FamilyABaseline(
    instrument_id="CRA-1.0",
    instrument_properties={"title": "Cyber Resilience Act"},
    practice_areas={_PA: _PA_PROPS},
    risk_paths={_RP: _RP_PROPS},
)
_DRIFTED = FamilyABaseline(
    instrument_id="CRA-1.0",
    instrument_properties={"title": "Cyber Resilience Act"},
    practice_areas={_PA: {**_PA_PROPS, "description": "incoming-should-not-apply"}},
    risk_paths={_RP: {**_RP_PROPS, "version": "9.9"}},
)
_OTHER_REGULATION = FamilyABaseline(
    instrument_id="NIS2-1.0",
    instrument_properties={"title": "NIS 2"},
    practice_areas={
        _PA: {"name": "Same id, other name", "status": "draft"},
        "pa_new": {"name": "N", "status": "active"},
    },
)
_SCENARIOS: dict[str, tuple[FamilyABaseline, ...]] = {
    "S-classification": (_FRESH,),
    "S-classification-rerun": (_FRESH, _FRESH),
    "S-classification-drifted": (_FRESH, _DRIFTED),
    "S-classification-overlap": (_FRESH, _OTHER_REGULATION),
}


class _DownGraph:
    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        del q, params
        raise redis.exceptions.ConnectionError("Error 111 connecting to 10.0.0.5:6379")


class _SpyGraph:
    """Delegates reads to a real handle and keeps every statement it was asked."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.statements: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        self.statements.append(q)
        return self._inner.query(q, params)  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownVariableType, reportUnknownMemberType]


@pytest.fixture
def emitter(make_emitter: MakeEmitter) -> LogEmitter:
    return make_emitter()[0]


def _merge(rig: MergeRig, baseline: FamilyABaseline, emitter: LogEmitter) -> None:
    merge_baseline_graph(
        baseline.instrument_id,
        baseline_graph=BaselineGraphReader(family_a_baseline_graph(baseline)),
        single_tenant_graph=rig.legacy,
        embed_model=EMBED_MODEL,
        similarity_threshold=SIMILARITY_THRESHOLD,
        emitter=emitter,
        **rig.merge_keywords(),
    )


def _target(rig: MergeRig) -> GraphWriteTarget:
    return GraphWriteTarget(rig.gateway, POLICY_SYSTEM, rig.audit_event_id)


def _classification_entries(rig: MergeRig) -> list[tuple[str, str]]:
    return [
        (e.name, e.identity)
        for g in rig.groups
        for e in g.entries
        if e.name in ("PracticeArea", "RiskPath")
    ]


def test_practice_areas_and_risk_paths_are_one_gateway_group_under_the_audit_event(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    _merge(merge_rig, _FRESH, emitter)

    groups = [
        g
        for g in merge_rig.groups
        if any(e.name in ("PracticeArea", "RiskPath") for e in g.entries)
    ]
    assert len(groups) == 1
    assert groups[0].audit_event_id == merge_rig.audit_event_id
    assert [(e.name, e.identity) for e in groups[0].entries] == [
        ("PracticeArea", _PA),
        ("RiskPath", _RP),
    ]
    assert merge_rig.policy_system.nodes[("PracticeArea", _PA)] == _PA_PROPS
    assert merge_rig.policy_system.nodes[("RiskPath", _RP)] == _RP_PROPS


def test_a_node_the_graph_holds_keeps_its_properties_and_nothing_is_logged_for_it(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    """Issue #106 AC-BI-007: the incoming node shares the id but carries other property values."""
    _merge(merge_rig, _FRESH, emitter)
    entries_before = len(_classification_entries(merge_rig))

    _merge(merge_rig, _DRIFTED, emitter)

    assert merge_rig.policy_system.nodes[("PracticeArea", _PA)] == _PA_PROPS
    assert merge_rig.policy_system.nodes[("RiskPath", _RP)] == _RP_PROPS
    assert len(_classification_entries(merge_rig)) == entries_before


def test_a_baseline_without_classification_content_reads_and_submits_nothing(
    merge_rig: MergeRig,
) -> None:
    spy = _SpyGraph(merge_rig.legacy)

    graph_writer.persist_practice_area_and_risk_path_passthrough(_target(merge_rig), spy, (), ())  # pyright: ignore[reportArgumentType]

    assert spy.statements == []
    assert merge_rig.groups == []


def test_only_one_label_present_reads_and_writes_only_that_label(merge_rig: MergeRig) -> None:
    spy = _SpyGraph(merge_rig.legacy)

    graph_writer.persist_practice_area_and_risk_path_passthrough(
        _target(merge_rig),
        spy,
        (),
        (BaselineNode(_RP, dict(_RP_PROPS)),),  # pyright: ignore[reportArgumentType]
    )

    assert len(spy.statements) == 1
    assert "RiskPath" in spy.statements[0]
    assert _classification_entries(merge_rig) == [("RiskPath", _RP)]


def test_a_repeated_id_in_the_input_keeps_the_first_occurrence(merge_rig: MergeRig) -> None:
    graph_writer.persist_practice_area_and_risk_path_passthrough(
        _target(merge_rig),
        merge_rig.legacy,
        (BaselineNode("pa_a", {"name": "first"}), BaselineNode("pa_a", {"name": "second"})),
        (),
    )

    assert merge_rig.policy_system.nodes[("PracticeArea", "pa_a")] == {"name": "first"}


def test_a_property_the_graph_cannot_store_raises_before_any_submit(merge_rig: MergeRig) -> None:
    with pytest.raises(CompanyMergePersistenceError, match=r"RiskPath 'rp_a'.*cannot store"):
        graph_writer.persist_practice_area_and_risk_path_passthrough(
            _target(merge_rig),
            merge_rig.legacy,
            (BaselineNode("pa_a", {"name": "ok"}),),
            (BaselineNode("rp_a", {"name": "x", "embedding": "reserved"}),),
        )

    assert merge_rig.groups == []


def test_a_failing_existing_node_read_is_query_failed_and_submits_nothing(
    merge_rig: MergeRig,
) -> None:
    with pytest.raises(CompanyMergePersistenceError, match=r"^FalkorDB query failed: "):
        graph_writer.persist_practice_area_and_risk_path_passthrough(
            _target(merge_rig), _DownGraph(), (BaselineNode("pa_a", {"name": "x"}),), ()
        )

    assert merge_rig.groups == []
    assert is_healthy(FALKORDB) is False


def test_the_endpoint_validation_read_failure_is_query_failed_and_marks_falkordb_unhealthy() -> (
    None
):
    with pytest.raises(CompanyMergePersistenceError, match=r"^FalkorDB query failed: "):
        graph_writer.validate_classification_edge_endpoints(
            _DownGraph(), (BareEdge("COVERS", "pa_a", "cap_a"),), {}
        )

    assert is_healthy(FALKORDB) is False


def test_the_endpoint_validation_sees_the_nodes_the_gateway_group_just_wrote(
    merge_rig: MergeRig,
) -> None:
    """The validation read follows the group: the PracticeArea it names exists once persisted."""
    graph_writer.persist_practice_area_and_risk_path_passthrough(
        _target(merge_rig), merge_rig.legacy, (BaselineNode(_PA, dict(_PA_PROPS)),), ()
    )

    graph_writer.validate_classification_edge_endpoints(
        merge_rig.legacy, (BareEdge("COVERS", _PA, "cap_a"),), {"cap_a": "cap_a"}
    )
    with pytest.raises(CompanyMergePersistenceError, match="not found in the single-tenant graph"):
        graph_writer.validate_classification_edge_endpoints(
            merge_rig.legacy, (BareEdge("COVERS", "pa_missing", "cap_a"),), {"cap_a": "cap_a"}
        )


def test_the_frozen_classification_statements_match_their_pinned_hashes() -> None:
    class _Capture:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def query(self, q: str, params: dict[str, object] | None = None) -> object:
            del params
            self.statements.append(q)

    capture = _Capture()

    write_legacy_practice_areas_and_risk_paths(
        capture, (BaselineNode("p", {}),), (BaselineNode("r", {}),)
    )

    assert [hashlib.sha256(s.encode()).hexdigest() for s in capture.statements] == [
        FROZEN_STATEMENT_HASHES["practice_area"],
        FROZEN_STATEMENT_HASHES["risk_path"],
    ]


@pytest.mark.parametrize("scenario", list(_SCENARIOS))
def test_the_frozen_reference_and_the_gateway_path_build_the_same_graph_for_classification_nodes(
    scenario: str, merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    """AC-BI-002, S-classification, modulo edge identity; live agreement not verifiable here."""
    steps = _SCENARIOS[scenario]
    reference = LegacyRecordingGraph()
    for step in steps:
        read = graph_reader.read_baseline_graph(
            BaselineGraphReader(family_a_baseline_graph(step)), step.instrument_id
        )
        write_legacy_instrument_roles_requirements(
            reference,
            step.instrument_id,
            read.regulatory_instrument_properties,
            read.role_nodes,
            read.requirement_nodes,
            read.provenance_edges,
        )
        write_legacy_practice_areas_and_risk_paths(
            reference, read.practice_area_nodes, read.risk_path_nodes
        )

    for step in steps:
        _merge(merge_rig, step, emitter)

    assert canonical_digest(merge_rig.policy_system) == canonical_digest(
        reference.as_gateway_graph()
    )


def test_the_classification_scenarios_exercise_what_they_claim_to() -> None:
    """Guard of the guard: the drifted and overlapping ids must reach the reference."""
    reference = LegacyRecordingGraph()
    for step in _SCENARIOS["S-classification-overlap"]:
        read = graph_reader.read_baseline_graph(
            BaselineGraphReader(family_a_baseline_graph(step)), step.instrument_id
        )
        write_legacy_practice_areas_and_risk_paths(
            reference, read.practice_area_nodes, read.risk_path_nodes
        )

    assert reference.nodes[("PracticeArea", _PA)]["name"] == "Secure SDLC"
    assert ("PracticeArea", "pa_new") in reference.nodes
