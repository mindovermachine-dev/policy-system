"""The gateway-backed cleanup builds the graph the frozen legacy writer built.

Each scenario seeds a fixture graph as a pre-gateway writer left it (identity-less edges), runs the
FROZEN legacy statement (`_legacy_reference`, against its interpreter) on a copy, runs the real
executor over the gateway rig on the other, and compares the two graphs by `canonical_digest`.
"Modulo edge identity": the legacy graph's edges are stamped by the same
`{type}:{source}->{target}` formula before the comparison, as the gateway stamps its own.

Shapes covered: release governance, obligation merge and obligation unmerge, the latter also as a
merge-then-unmerge round trip. The Capability merge and unmerge are not compared with the frozen
legacy statements: they intentionally diverge (the absorbed node is deleted and a
`MergedCapability` marker remains, where the legacy writer kept a `merged` tombstone), so they are
proven on their own terms in `test_capability_merge_gateway.py`,
`test_capability_unmerge_gateway.py` and `test_cleanup_round_trip.py`. Met
modulo edge identity; the live digest and live interpreter agreement are not verifiable here (no
FalkorDB in this sandbox): the `falkordb_live` tests stay as the live anchor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from authz._fakes import FakeAccessRoleStore

from graph_cleanup._fakes import (
    OBL_ABSORBED,
    OBL_SURVIVOR,
    SURVIVOR,
    obligation_merge_history,
)
from graph_cleanup._gateway_fakes import OTHER_CAPABILITY, POLICY, CleanupRig
from graph_cleanup._legacy_reference import (
    LegacyRecordingGraph,
    legacy_merge_obligations,
    legacy_release_governance,
    legacy_unmerge_obligation,
)
from graph_cleanup._merge_runs import (
    obligation_plan,
    obligation_row,
    run_obligation_merge,
)
from graph_cleanup._unmerge_runs import (
    obligation_unmerge_world,
    run_obligation_unmerge,
    unmerge_plan,
    unmerge_row,
)
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.executors import TOOL_RELEASE_GOVERNANCE, execute_release_governance
from ps_service.graph_cleanup.graph_reader import read_release_state
from ps_service.graph_cleanup.models import ObligationUnmergePlan
from ps_service.graph_cleanup.release_planner import plan_release_governance
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.logging import configure
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from collections.abc import Callable

_OWNER = ("owner", "https://issuer.example.com/")
_OFFICER = ("officer", "https://issuer.example.com/")
_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=False,
)


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _roles() -> FakeAccessRoleStore:
    store = FakeAccessRoleStore(expected_owner=_OWNER)
    store.bootstrap_first_owner(_OWNER)
    store.grant(actor=_OWNER, target=_OFFICER, access_role=AccessRole.COMPLIANCE_OFFICER)
    return store


def _legacy_digest(rig: CleanupRig, run: Callable[[LegacyRecordingGraph], object]) -> str:
    legacy = LegacyRecordingGraph.copy_of(rig.graph)
    run(legacy)
    return canonical_digest(legacy.as_gateway_graph())


def _release(rig: CleanupRig) -> dict[str, object]:
    state = read_release_state(rig.read, capability_id=SURVIVOR)
    now = datetime.now(UTC)
    row = PendingApprovalRow(
        id="approval-1",
        code_hash=b"h",
        tool_name=TOOL_RELEASE_GOVERNANCE,
        normalized_args={
            "capability_id": SURVIVOR,
            "policy_id": POLICY,
            "state_digest": plan_release_governance(state).state_digest,
        },
        actor_subject=_OFFICER[0],
        actor_issuer=_OFFICER[1],
        nonce=b"n",
        display_summary={},
        status="signed",
        outcome=None,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    return execute_release_governance(row, _CONFIG, rig.dependencies(_roles()))


def _seed_siblings(rig: CleanupRig) -> None:
    """A second Policy and Capability the release must leave alone."""
    rig.seed_capability("cap_sibling", name="Sibling", status="active")
    rig.seed_policy("pol_2", title="Other Policy", status="approved")
    rig.seed_governs("cap_sibling", "pol_2", legacy=True)


@pytest.mark.parametrize("siblings", [False, True], ids=["bare", "with-siblings"])
def test_a_release_leaves_the_graph_the_frozen_legacy_statement_left(
    cleanup_rig: CleanupRig, *, siblings: bool
) -> None:
    cleanup_rig.seed_release_world(legacy=True)
    if siblings:
        _seed_siblings(cleanup_rig)
    expected = _legacy_digest(
        cleanup_rig,
        lambda legacy: legacy_release_governance(legacy, capability_id=SURVIVOR, policy_id=POLICY),
    )

    outcome = _release(cleanup_rig)

    assert outcome["released"] is True
    assert canonical_digest(cleanup_rig.graph) == expected


def test_a_release_of_identified_edges_leaves_the_same_graph(cleanup_rig: CleanupRig) -> None:
    cleanup_rig.seed_release_world(legacy=False)
    expected = _legacy_digest(
        cleanup_rig,
        lambda legacy: legacy_release_governance(legacy, capability_id=SURVIVOR, policy_id=POLICY),
    )

    _release(cleanup_rig)

    assert canonical_digest(cleanup_rig.graph) == expected


def test_a_release_of_the_last_governed_capability_leaves_the_same_graph(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_release_world(legacy=True)
    for key in [k for k in cleanup_rig.graph.edges if k[2] == OTHER_CAPABILITY]:
        del cleanup_rig.graph.edges[key]
    expected = _legacy_digest(
        cleanup_rig,
        lambda legacy: legacy_release_governance(legacy, capability_id=SURVIVOR, policy_id=POLICY),
    )

    _release(cleanup_rig)

    assert canonical_digest(cleanup_rig.graph) == expected


def test_a_guard_miss_writes_nothing_as_the_frozen_statement_matches_nothing(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_release_world(legacy=True)
    after_the_change: list[str] = []

    def _policy_leaves_draft() -> None:
        cleanup_rig.graph.nodes[("Policy", POLICY)].update(status="approved")
        after_the_change.append(canonical_digest(cleanup_rig.graph))

    cleanup_rig.read.flip_before("p.title", 2, _policy_leaves_draft)
    legacy = LegacyRecordingGraph.copy_of(cleanup_rig.graph)
    legacy.nodes[("Policy", POLICY)]["status"] = "approved"
    assert legacy_release_governance(legacy, capability_id=SURVIVOR, policy_id=POLICY) is False

    outcome = _release(cleanup_rig)

    assert "error" in outcome
    assert canonical_digest(cleanup_rig.graph) == after_the_change[0]


# -- merge-obligations (S4) -----------------------------------------------------------------------


def _legacy_obligation_merge(rig: CleanupRig) -> Callable[[LegacyRecordingGraph], bool]:
    expected = obligation_plan(rig).expected
    return lambda legacy: legacy_merge_obligations(
        legacy, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED, expected=expected
    )


@pytest.mark.parametrize("legacy_edges", [True, False], ids=["legacy-edges", "identified-edges"])
def test_an_obligation_merge_leaves_the_graph_the_frozen_legacy_statement_left(
    cleanup_rig: CleanupRig, *, legacy_edges: bool
) -> None:
    cleanup_rig.seed_obligation_merge_world(legacy=legacy_edges)
    expected = _legacy_digest(cleanup_rig, _legacy_obligation_merge(cleanup_rig))

    outcome = run_obligation_merge(cleanup_rig)

    assert outcome["merged"] is True
    assert canonical_digest(cleanup_rig.graph) == expected


def test_an_obligation_merge_of_an_absorbed_obligation_with_no_edges_leaves_the_same_graph(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_obligation_merge_world(legacy=True)
    for key in [
        k for k in cleanup_rig.graph.edges if k[0] != "HAS" and OBL_ABSORBED in {k[2], k[4]}
    ]:
        del cleanup_rig.graph.edges[key]
    expected = _legacy_digest(cleanup_rig, _legacy_obligation_merge(cleanup_rig))

    run_obligation_merge(cleanup_rig)

    assert canonical_digest(cleanup_rig.graph) == expected


def test_an_obligation_merge_where_the_survivor_already_has_every_edge_leaves_the_same_graph(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_obligation_merge_world(legacy=True)
    cleanup_rig.seed_edge(
        ("SATISFIED_BY", "Requirement", "req_1", "Obligation", OBL_SURVIVOR), legacy=True
    )
    expected = _legacy_digest(cleanup_rig, _legacy_obligation_merge(cleanup_rig))

    run_obligation_merge(cleanup_rig)

    assert canonical_digest(cleanup_rig.graph) == expected


def test_an_obligation_merge_leaves_other_roles_and_obligations_alone_as_the_statement_did(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_obligation_merge_world(legacy=True)
    cleanup_rig.seed_endpoint("Role", "role_2", name="Importer")
    cleanup_rig.seed_endpoint("Obligation", "obl_other", text="Other", confidence=0.5)
    cleanup_rig.seed_edge(("HAS", "Role", "role_2", "Obligation", "obl_other"), legacy=True)
    cleanup_rig.seed_edge(
        ("REQUIRES", "Obligation", "obl_other", "Capability", "cap_1"), legacy=True
    )
    expected = _legacy_digest(cleanup_rig, _legacy_obligation_merge(cleanup_rig))

    run_obligation_merge(cleanup_rig)

    assert canonical_digest(cleanup_rig.graph) == expected


def test_an_obligation_merge_guard_miss_writes_nothing_as_the_frozen_statement_matches_nothing(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_obligation_merge_world(legacy=True)
    cleanup_rig.seed_endpoint("Capability", "cap_new", name="cap_new", status="active")
    row = obligation_row(cleanup_rig)
    previewed = obligation_plan(cleanup_rig).expected
    changed: list[str] = []

    def _a_capability_is_required() -> None:
        cleanup_rig.seed_edge(
            ("REQUIRES", "Obligation", OBL_ABSORBED, "Capability", "cap_new"), legacy=True
        )
        changed.append(canonical_digest(cleanup_rig.graph))

    cleanup_rig.read.flip_before("RETURN o.id, o.text", 2, _a_capability_is_required)
    outcome = run_obligation_merge(cleanup_rig, row=row)

    assert "error" in outcome
    assert canonical_digest(cleanup_rig.graph) == changed[0]
    legacy = LegacyRecordingGraph.copy_of(cleanup_rig.graph)
    assert (
        legacy_merge_obligations(
            legacy, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED, expected=previewed
        )
        is False
    )


# -- unmerge-obligation (S6) ----------------------------------------------------------------------


def _legacy_obligation_unmerge(rig: CleanupRig) -> Callable[[LegacyRecordingGraph], bool]:
    plan = unmerge_plan(rig, OBL_ABSORBED)
    assert isinstance(plan, ObligationUnmergePlan)
    return lambda legacy: legacy_unmerge_obligation(
        legacy, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED, write=plan.write
    )


@pytest.mark.parametrize("legacy_edges", [True, False], ids=["legacy-edges", "identified-edges"])
@pytest.mark.parametrize("added", [False, True], ids=["plain", "survivor-added-edge"])
def test_an_obligation_unmerge_leaves_the_graph_the_frozen_legacy_statement_left(
    cleanup_rig: CleanupRig, *, legacy_edges: bool, added: bool
) -> None:
    obligation_unmerge_world(cleanup_rig, legacy=legacy_edges)
    if added:
        cleanup_rig.seed_endpoint("Capability", "cap_new", name="cap_new", status="active")
        cleanup_rig.seed_edge(
            ("REQUIRES", "Obligation", OBL_SURVIVOR, "Capability", "cap_new"), legacy=legacy_edges
        )
    expected = _legacy_digest(cleanup_rig, _legacy_obligation_unmerge(cleanup_rig))

    outcome = run_obligation_unmerge(cleanup_rig)

    assert outcome["unmerged"] is True
    assert canonical_digest(cleanup_rig.graph) == expected


def test_an_obligation_unmerge_leaves_other_roles_and_obligations_alone_as_the_statement_did(
    cleanup_rig: CleanupRig,
) -> None:
    obligation_unmerge_world(cleanup_rig, legacy=True)
    cleanup_rig.seed_endpoint("Role", "role_2", name="Importer")
    cleanup_rig.seed_endpoint("Obligation", "obl_other", text="Other", confidence=0.5)
    cleanup_rig.seed_edge(("HAS", "Role", "role_2", "Obligation", "obl_other"), legacy=True)
    cleanup_rig.seed_edge(
        ("SATISFIED_BY", "Requirement", "req_1", "Obligation", "obl_other"), legacy=True
    )
    expected = _legacy_digest(cleanup_rig, _legacy_obligation_unmerge(cleanup_rig))

    run_obligation_unmerge(cleanup_rig)

    assert canonical_digest(cleanup_rig.graph) == expected


def test_an_obligation_unmerge_guard_miss_writes_nothing_as_the_frozen_statement_matches_nothing(
    cleanup_rig: CleanupRig,
) -> None:
    obligation_unmerge_world(cleanup_rig, legacy=True)
    row = unmerge_row(cleanup_rig, OBL_ABSORBED)
    plan = unmerge_plan(cleanup_rig, OBL_ABSORBED)
    assert isinstance(plan, ObligationUnmergePlan)
    changed: list[str] = []

    def _a_requirement_disappears() -> None:
        del cleanup_rig.graph.nodes[("Requirement", "req_1")]
        changed.append(canonical_digest(cleanup_rig.graph))

    cleanup_rig.read.flip_before("RETURN o.id, o.text", 2, _a_requirement_disappears)
    outcome = run_obligation_unmerge(cleanup_rig, row=row)

    assert "error" in outcome
    assert canonical_digest(cleanup_rig.graph) == changed[0]
    legacy = LegacyRecordingGraph.copy_of(cleanup_rig.graph)
    assert (
        legacy_unmerge_obligation(
            legacy, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED, write=plan.write
        )
        is False
    )


@pytest.mark.parametrize("added", [False, True], ids=["plain", "survivor-added-edge"])
def test_an_obligation_merge_then_unmerge_round_trip_matches_the_frozen_round_trip(
    cleanup_rig: CleanupRig, *, added: bool
) -> None:
    cleanup_rig.seed_obligation_merge_world(legacy=True)
    original = LegacyRecordingGraph.copy_of(cleanup_rig.graph)
    frozen = LegacyRecordingGraph.copy_of(cleanup_rig.graph)
    assert _legacy_obligation_merge(cleanup_rig)(frozen)
    assert run_obligation_merge(cleanup_rig)["merged"] is True
    cleanup_rig.audit.history = obligation_merge_history()
    if added:
        cleanup_rig.seed_endpoint("Capability", "cap_new", name="cap_new", status="active")
        frozen.nodes[("Capability", "cap_new")] = {"name": "cap_new", "status": "active"}
        for key in (("REQUIRES", "Obligation", OBL_SURVIVOR, "Capability", "cap_new"),):
            cleanup_rig.seed_edge(key, legacy=False)
            frozen.edges[key] = {}
    assert _legacy_obligation_unmerge(cleanup_rig)(frozen)

    assert run_obligation_unmerge(cleanup_rig)["unmerged"] is True

    assert canonical_digest(cleanup_rig.graph) == canonical_digest(frozen.as_gateway_graph())
    # A union cannot be attributed, so the survivor keeps what the merge gave it: the round trip
    # differs from the original by exactly that one edge (and by the edge it gained, when it did).
    gained = {(k[0], k[2], k[4]) for k in cleanup_rig.graph.edges} - {
        (k[0], k[2], k[4]) for k in original.edges
    }
    assert gained == {("SATISFIED_BY", "req_1", OBL_SURVIVOR)} | (
        {("REQUIRES", OBL_SURVIVOR, "cap_new")} if added else set()
    )
