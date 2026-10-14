"""`merge_baseline_graph` writes the rewired edges through the Graph Write Gateway.

The ten Edge Catalog types leave Company Merge as ONE gateway group on
`policy_system`, after the node groups and before the embedding backfills. Equivalence
against the frozen legacy reference (`MATCH ... MERGE`, no identity) is met modulo edge identity,
with a scenario for a REQUIRES target rewritten onto a matched Capability (S-redirect), for a
dangling edge, and for a re-run; live digest and live interpreter agreement are not verifiable
here (no FalkorDB in this sandbox).

7b: a `policy_system` edge written before the gateway has no identity and is stamped once, never
duplicated; the validation of classification edges can still raise after the node groups were
applied (the observable partial-write order).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from company_merge._gateway_fakes import (
    FamilyABaseline,
    MergeRig,
)
from company_merge._legacy_reference import FROZEN_STATEMENT_HASHES, write_legacy_rewired_edges
from company_merge._merge_equivalence import production_run, reference_graph
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.models import BareEdge
from ps_service.graph_gateway.digest import canonical_digest

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from ps_service.graph_gateway.edge_identity_backfill import EdgeIdentityBackfillOutcome
    from ps_service.graph_gateway.models import GroupOutcome, MutationGroup
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.logging import LogEmitter

_ROLE = "role_manufacturer"
_OBL = "obl_notify_the_csirt"
_OBL_B = "obl_keep_records"
_REQ = "req_a"
_CAP_A = "capability_encrypt_data"
_CAP_ALIAS = "capability_encrypt_data_alias"
_ALL_EDGE_TYPES = ("HAS", "SATISFIED_BY", "REQUIRES")

_FIRST = FamilyABaseline(
    instrument_id="CRA-1.0",
    instrument_properties={"title": "Cyber Resilience Act"},
    roles={_ROLE: "Manufacturer"},
    requirements={_REQ: "Conduct a risk assessment"},
    obligations={_OBL: ("Notify the CSIRT.", 0.7)},
    capabilities={_CAP_A: ("Encrypt data", 0.8)},
    edges=(
        ("HAS", "Role", _ROLE, "Obligation", _OBL),
        ("SATISFIED_BY", "Requirement", _REQ, "Obligation", _OBL),
        ("REQUIRES", "Obligation", _OBL, "Capability", _CAP_A),
    ),
)
# Same Capability under another id but the same name: dedup matches it semantically onto the first
# regulation's node, so this REQUIRES target must be rewritten to `_CAP_A` (S-redirect).
_SECOND_SEMANTIC = FamilyABaseline(
    instrument_id="NIS2-1.0",
    instrument_properties={"title": "NIS 2"},
    roles={"role_operator": "Operator"},
    obligations={_OBL_B: ("Keep records.", 0.6)},
    capabilities={_CAP_ALIAS: ("Encrypt data", 0.8)},
    edges=(
        ("HAS", "Role", "role_operator", "Obligation", _OBL_B),
        ("REQUIRES", "Obligation", _OBL_B, "Capability", _CAP_ALIAS),
    ),
)
# The same Capability id again: matched exactly, so the rewrite maps it onto itself.
_SECOND_EXACT = FamilyABaseline(
    instrument_id="NIS2-1.0",
    instrument_properties={"title": "NIS 2"},
    obligations={_OBL_B: ("Keep records.", 0.6)},
    capabilities={_CAP_A: ("Encrypt data", 0.8)},
    edges=(("REQUIRES", "Obligation", _OBL_B, "Capability", _CAP_A),),
)
# Edges whose endpoints are in neither the baseline nor the graph: the old MATCH wrote nothing.
_DANGLING = FamilyABaseline(
    instrument_id="DORA-1.0",
    instrument_properties={"title": "DORA"},
    roles={_ROLE: "Manufacturer"},
    obligations={_OBL: ("Notify the CSIRT.", 0.7)},
    capabilities={_CAP_A: ("Encrypt data", 0.8)},
    edges=(
        ("HAS", "Role", _ROLE, "Obligation", "obl_ghost"),
        ("HAS", "Role", "role_ghost", "Obligation", _OBL),
        ("SATISFIED_BY", "Requirement", "req_ghost", "Obligation", _OBL),
        ("REQUIRES", "Obligation", _OBL, "Capability", _CAP_A),
    ),
)
_SCENARIOS: dict[str, tuple[FamilyABaseline, ...]] = {
    "S-redirect-fresh": (_FIRST,),
    "S-redirect-rerun": (_FIRST, _FIRST),
    "S-redirect-exact": (_FIRST, _SECOND_EXACT),
    "S-redirect-semantic": (_FIRST, _SECOND_SEMANTIC),
    "S-redirect-dangling": (_DANGLING,),
}


class _SpySubmitter:
    """Delegates to the rig's gateway and records which gateway calls were made, in order."""

    def __init__(self, gateway: GroupSubmitter) -> None:
        self._gateway = gateway
        self.calls: list[str] = []

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        self.calls.append("submit")
        return self._gateway.submit_group(group)

    def is_caught_up(self, graph: str) -> bool:
        self.calls.append("caught_up")
        return self._gateway.is_caught_up(graph)

    def stamp_legacy_edge_identity(
        self, graph: str, audit_event_id: str | None
    ) -> EdgeIdentityBackfillOutcome:
        self.calls.append("stamp")
        return self._gateway.stamp_legacy_edge_identity(graph, audit_event_id)


@pytest.fixture
def emitter(make_emitter: MakeEmitter) -> LogEmitter:
    return make_emitter()[0]


@pytest.mark.parametrize("scenario", list(_SCENARIOS))
def test_the_frozen_reference_and_the_gateway_path_build_the_same_graph_for_rewired_edges(
    scenario: str, merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    """Family E (rewired edges), same generated values; modulo edge identity."""
    steps = _SCENARIOS[scenario]

    reference = reference_graph(steps, emitter)
    production_run(steps, merge_rig, emitter)

    assert canonical_digest(merge_rig.policy_system) == canonical_digest(
        reference.as_gateway_graph()
    )


def test_the_scenarios_exercise_what_they_claim_to(emitter: LogEmitter) -> None:
    """Guard of the guard: the rewrite and the dropped edges really reach the reference."""
    semantic = reference_graph(_SCENARIOS["S-redirect-semantic"], emitter)
    dangling = reference_graph(_SCENARIOS["S-redirect-dangling"], emitter)

    # the alias Capability is never a node; its REQUIRES lands on the first regulation's one
    assert ("Capability", _CAP_ALIAS) not in semantic.nodes
    assert ("REQUIRES", "Obligation", _OBL_B, "Capability", _CAP_A) in semantic.edges
    # four bare edges were issued, only the REQUIRES had both endpoints
    assert len(dangling.edge_calls) == 4 + 0  # provenance edges are none in this baseline
    assert set(dangling.edges) == {("REQUIRES", "Obligation", _OBL, "Capability", _CAP_A)}


def test_the_matched_capability_target_is_rewritten_to_the_canonical_id_in_the_group(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    production_run(_SCENARIOS["S-redirect-semantic"], merge_rig, emitter)

    requires = [e.identity for e in merge_rig.logged() if e.name == "REQUIRES"]
    assert requires == [f"REQUIRES:{_OBL}->{_CAP_A}", f"REQUIRES:{_OBL_B}->{_CAP_A}"]
    assert ("Capability", _CAP_ALIAS) not in merge_rig.policy_system.nodes


def test_the_edges_are_one_group_after_the_node_groups_and_before_the_backfills(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    production_run(_SCENARIOS["S-redirect-semantic"], merge_rig, emitter)

    kinds = [{e.name for e in g.entries} for g in merge_rig.groups]
    edge_groups = [i for i, names in enumerate(kinds) if names & set(_ALL_EDGE_TYPES)]
    capability_groups = [i for i, names in enumerate(kinds) if "Capability" in names]
    # first regulation: nodes, then its edges; second: its nodes (no Capability: matched), edges
    assert len(edge_groups) == 2
    assert all(
        {e.name for e in merge_rig.groups[i].entries} <= set(_ALL_EDGE_TYPES) | {"DEFINES"}
        for i in edge_groups
    )
    assert capability_groups[0] < edge_groups[0] < edge_groups[1]
    assert all(g.audit_event_id == merge_rig.audit_event_id for g in merge_rig.groups)


def test_rerunning_the_merge_adds_no_parallel_edge_and_logs_no_edge_entry(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    production_run((_FIRST,), merge_rig, emitter)
    logged = len(merge_rig.logged())
    edges = len(merge_rig.policy_system.edges)

    production_run((_FIRST,), merge_rig, emitter)

    assert len(merge_rig.policy_system.edges) == edges
    assert len(merge_rig.logged()) == logged


def test_the_frozen_rewired_edge_statement_matches_its_pinned_hash() -> None:
    class _Capture:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def query(self, q: str, params: dict[str, object] | None = None) -> object:
            del params
            self.statements.append(q)

    capture = _Capture()

    write_legacy_rewired_edges(capture, (BareEdge("HAS", "r", "o"),), {})

    assert [hashlib.sha256(s.encode()).hexdigest() for s in capture.statements] == [
        FROZEN_STATEMENT_HASHES["rewired_edge"]
    ]


def test_a_legacy_policy_system_edge_without_identity_is_stamped_not_duplicated(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    """7b: an identity-less `policy_system` edge gets its identity from ONE stamp group, before the
    merge's first group, under the run's audit event; the rewired edge then merges into it.
    """
    for label, node_id in (("Role", _ROLE), ("Obligation", _OBL)):
        merge_rig.policy_system.seed_node((label,), node_id, {})
    merge_rig.policy_system.seed_legacy_edge(("HAS", "Role", _ROLE, "Obligation", _OBL), {})
    spy = _SpySubmitter(merge_rig.gateway)

    production_run((_FIRST,), merge_rig, emitter, submitter=spy)

    assert spy.calls.count("stamp") == 1
    assert spy.calls[0] == "stamp"
    has = [key for key in merge_rig.policy_system.edges if key[0] == "HAS"]
    assert has == [("HAS", "Role", _ROLE, "Obligation", _OBL, f"HAS:{_ROLE}->{_OBL}")]
    # the stamp is the one logged mention of the edge: the rewired upsert merged into it unchanged
    mentions = [g for g in merge_rig.groups if any(e.name == "HAS" for e in g.entries)]
    assert mentions == [merge_rig.groups[0]]
    assert merge_rig.groups[0].audit_event_id == merge_rig.audit_event_id


def test_classification_endpoint_validation_still_raises_after_the_node_groups_were_applied(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    """The validation read runs after the node groups, so a run that fails it
    leaves the nodes written and no edge (this is observable and unchanged by the gateway).
    """
    broken = FamilyABaseline(
        instrument_id="CRA-1.0",
        instrument_properties={"title": "Cyber Resilience Act"},
        roles={_ROLE: "Manufacturer"},
        obligations={_OBL: ("Notify the CSIRT.", 0.7)},
        capabilities={_CAP_A: ("Encrypt data", 0.8)},
        practice_areas={"pa_secure_sdlc": {"name": "Secure SDLC", "status": "active"}},
        edges=(
            ("HAS", "Role", _ROLE, "Obligation", _OBL),
            ("COVERS", "PracticeArea", "pa_secure_sdlc", "Capability", "cap_never_persisted"),
        ),
    )

    with pytest.raises(CompanyMergePersistenceError, match="cap_never_persisted"):
        production_run((broken,), merge_rig, emitter)

    nodes = set(merge_rig.policy_system.nodes)
    assert {
        ("RegulatoryInstrument", "CRA-1.0"),
        ("Role", _ROLE),
        ("Obligation", _OBL),
        ("Capability", _CAP_A),
        ("PracticeArea", "pa_secure_sdlc"),
    } <= nodes
    assert merge_rig.policy_system.edges == {}
    assert not any(e.name in {"HAS", "COVERS"} for e in merge_rig.logged())
