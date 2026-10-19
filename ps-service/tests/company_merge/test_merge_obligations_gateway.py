"""`merge_baseline_graph` writes the Obligation nodes through the Graph Write Gateway.

An Obligation is a create-only passthrough node. Create-only is decided
BEFORE submission from one batched existing-node read, so an Obligation the graph already holds
(however much its incoming `confidence` has drifted) is left out of the group: it keeps every
property and nothing is logged for it. The net-new count is read through the same read
wrapper as every other Company Merge read.

Equivalence against the frozen legacy reference is met modulo edge identity; live
digest and live interpreter agreement are not verifiable here (no FalkorDB in this sandbox).
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
    write_legacy_obligations,
)
from company_merge._seed_merge_graphs import (
    EMBED_MODEL,
    MERGE_THRESHOLD,
    BaselineGraphReader,
    SingleTenantStore,
)
from company_merge._thresholds import REVIEW_THRESHOLD
from ps_service.company_merge import graph_reader, graph_writer
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.models import BaselineNode
from ps_service.dependency_health import FALKORDB, is_healthy
from ps_service.graph_gateway.digest import canonical_digest

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from ps_service.company_merge.falkordb_client import GraphQueryResult
    from ps_service.logging import LogEmitter

_OBLIGATION_ID = "obl_ensure_correct_application_of_the_ce_marking_regime_0579d8"
_CRA = FamilyABaseline(
    instrument_id="CRA-1.0",
    instrument_properties={"title": "Cyber Resilience Act"},
    roles={"role_manufacturer": "Manufacturer"},
    obligations={_OBLIGATION_ID: ("Ensure correct application of the CE marking regime.", 0.88)},
)
_CRA_DRIFTED = FamilyABaseline(
    instrument_id="CRA-1.0",
    instrument_properties={"title": "Cyber Resilience Act"},
    roles={"role_manufacturer": "Manufacturer"},
    obligations={_OBLIGATION_ID: ("Ensure correct application of the CE marking regime.", 0.93)},
)
_NIS2_SHARING = FamilyABaseline(
    instrument_id="NIS2-1.0",
    instrument_properties={"title": "NIS 2"},
    obligations={
        _OBLIGATION_ID: ("Same id, other text", 0.5),
        "obl_notify_the_csirt": ("Notify the CSIRT.", 0.7),
    },
)
_SCENARIOS: dict[str, tuple[FamilyABaseline, ...]] = {
    "S-obligation-fresh": (_CRA,),
    "S-obligation-rerun": (_CRA, _CRA),
    "S-obligation-drifted": (_CRA, _CRA_DRIFTED),
    "S-obligation-overlap": (_CRA, _NIS2_SHARING),
}


class _DownGraph:
    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        del q, params
        raise redis.exceptions.ConnectionError("Error 111 connecting to 10.0.0.5:6379")


@pytest.fixture
def emitter(make_emitter: MakeEmitter) -> LogEmitter:
    return make_emitter()[0]


def _merge(rig: MergeRig, baseline: FamilyABaseline, emitter: LogEmitter) -> None:
    merge_baseline_graph(
        baseline.instrument_id,
        baseline_graph=BaselineGraphReader(family_a_baseline_graph(baseline)),
        single_tenant_graph=rig.legacy,
        embed_model=EMBED_MODEL,
        merge_threshold=MERGE_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        emitter=emitter,
        **rig.merge_keywords(),
    )


def _target(rig: MergeRig) -> GraphWriteTarget:
    return GraphWriteTarget(rig.gateway, POLICY_SYSTEM, rig.audit_event_id)


def test_new_obligations_are_one_gateway_group_under_the_audit_event(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    _merge(merge_rig, _NIS2_SHARING, emitter)

    obligation_groups = [
        g for g in merge_rig.groups if any(e.name == "Obligation" for e in g.entries)
    ]
    assert len(obligation_groups) == 1
    group = obligation_groups[0]
    assert group.audit_event_id == merge_rig.audit_event_id
    assert [(e.name, e.identity) for e in group.entries] == [
        ("Obligation", _OBLIGATION_ID),
        ("Obligation", "obl_notify_the_csirt"),
    ]
    assert merge_rig.policy_system.nodes[("Obligation", "obl_notify_the_csirt")] == {
        "text": "Notify the CSIRT.",
        "confidence": 0.7,
    }


def test_an_obligation_the_graph_holds_keeps_its_properties_and_nothing_is_logged_for_it(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    """IMPL_SLICE_7's live finding: 199 Obligations drifted `confidence` on a second merge."""
    _merge(merge_rig, _CRA, emitter)
    logged_before = len(merge_rig.logged())

    _merge(merge_rig, _CRA_DRIFTED, emitter)

    assert merge_rig.policy_system.nodes[("Obligation", _OBLIGATION_ID)] == {
        "text": "Ensure correct application of the CE marking regime.",
        "confidence": 0.88,
    }
    obligation_entries = [e for e in merge_rig.logged()[logged_before:] if e.name == "Obligation"]
    assert obligation_entries == []


def test_a_repeated_obligation_id_in_the_input_keeps_the_first_occurrence(
    merge_rig: MergeRig,
) -> None:
    graph_writer.persist_obligation_passthrough(
        _target(merge_rig),
        merge_rig.legacy,
        (BaselineNode("obl_a", {"text": "first"}), BaselineNode("obl_a", {"text": "second"})),
    )

    assert merge_rig.policy_system.nodes[("Obligation", "obl_a")] == {"text": "first"}


def test_no_obligations_submits_nothing(merge_rig: MergeRig) -> None:
    graph_writer.persist_obligation_passthrough(_target(merge_rig), merge_rig.legacy, ())

    assert merge_rig.groups == []


def test_an_obligation_property_the_graph_cannot_store_raises_before_any_submit(
    merge_rig: MergeRig,
) -> None:
    with pytest.raises(CompanyMergePersistenceError, match=r"Obligation 'obl_a'.*cannot store"):
        graph_writer.persist_obligation_passthrough(
            _target(merge_rig),
            merge_rig.legacy,
            (BaselineNode("obl_a", {"text": "x", "confidence": float("nan")}),),
        )

    assert merge_rig.groups == []


def test_a_failing_existing_obligation_read_is_query_failed_and_submits_nothing(
    merge_rig: MergeRig,
) -> None:
    with pytest.raises(CompanyMergePersistenceError, match=r"^FalkorDB query failed: "):
        graph_writer.persist_obligation_passthrough(
            _target(merge_rig), _DownGraph(), (BaselineNode("obl_a", {"text": "x"}),)
        )

    assert merge_rig.groups == []
    assert is_healthy(FALKORDB) is False


def test_the_new_obligation_count_is_read_through_the_read_wrapper(merge_rig: MergeRig) -> None:
    with pytest.raises(CompanyMergePersistenceError, match=r"^FalkorDB query failed: "):
        graph_writer.count_new_obligations(_DownGraph(), ("obl_a",))

    merge_rig.policy_system.seed_node(("Obligation",), "obl_a", {"text": "x"})
    assert graph_writer.count_new_obligations(merge_rig.legacy, ("obl_a", "obl_b", "obl_b")) == 1
    assert graph_writer.count_new_obligations(merge_rig.legacy, ()) == 0


def test_the_net_new_obligation_count_is_a_delta_the_frozen_reference_gives_too(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    """A second run over the same Obligation reports 0 new, on the gateway AND the legacy path.

    IMPL_SLICE_7a's open item: `test_merge_idempotency`'s single-tenant fake once never answered
    the count read (every id looked new), so a repeated run seemed to report 1 again. Against the
    graph the frozen writer builds (create-only upsert, then the same unchanged count read) the
    second count is 0, so the gateway path and legacy agree; the old 1 was the fake's blindness.
    """
    reference = LegacyRecordingGraph()
    reads = SingleTenantStore(nodes=reference.nodes)
    read = graph_reader.read_baseline_graph(
        BaselineGraphReader(family_a_baseline_graph(_CRA)), _CRA.instrument_id
    )
    ids = tuple(node.id for node in read.obligation_nodes)
    legacy_counts = [graph_writer.count_new_obligations(reads, ids)]
    write_legacy_obligations(reference, read.obligation_nodes)
    legacy_counts.append(graph_writer.count_new_obligations(reads, ids))

    gateway_counts: list[int] = []
    for _ in range(2):
        merge = merge_baseline_graph(
            _CRA.instrument_id,
            baseline_graph=BaselineGraphReader(family_a_baseline_graph(_CRA)),
            single_tenant_graph=merge_rig.legacy,
            embed_model=EMBED_MODEL,
            merge_threshold=MERGE_THRESHOLD,
            review_threshold=REVIEW_THRESHOLD,
            emitter=emitter,
            **merge_rig.merge_keywords(),
        )
        gateway_counts.append(merge.new_obligation_count)

    assert legacy_counts == [1, 0]
    assert gateway_counts == legacy_counts


def test_the_frozen_obligation_statement_matches_its_pinned_hash() -> None:
    class _Capture:
        statements: list[str]

        def __init__(self) -> None:
            self.statements = []

        def query(self, q: str, params: dict[str, object] | None = None) -> object:
            del params
            self.statements.append(q)

    capture = _Capture()

    write_legacy_obligations(capture, (BaselineNode("o", {}),))

    assert [hashlib.sha256(s.encode()).hexdigest() for s in capture.statements] == [
        FROZEN_STATEMENT_HASHES["obligation"]
    ]


@pytest.mark.parametrize("scenario", list(_SCENARIOS))
def test_the_frozen_reference_and_the_gateway_path_build_the_same_graph_for_obligations(
    scenario: str, merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    """Family B (Obligation), modulo edge identity; live agreement not verifiable."""
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
        write_legacy_obligations(reference, read.obligation_nodes)

    for step in steps:
        _merge(merge_rig, step, emitter)

    assert canonical_digest(merge_rig.policy_system) == canonical_digest(
        reference.as_gateway_graph()
    )


def test_the_obligation_scenarios_exercise_what_they_claim_to() -> None:
    """Guard of the guard: drift and overlap must actually reach the reference."""
    reference = LegacyRecordingGraph()
    for step in _SCENARIOS["S-obligation-overlap"]:
        read = graph_reader.read_baseline_graph(
            BaselineGraphReader(family_a_baseline_graph(step)), step.instrument_id
        )
        write_legacy_obligations(reference, read.obligation_nodes)

    assert reference.nodes[("Obligation", _OBLIGATION_ID)]["confidence"] == 0.88
    assert ("Obligation", "obl_notify_the_csirt") in reference.nodes
