"""A near-miss merge signed against one graph state is refused when that state has moved.

If either Capability, its status, an edge count, its governing Policy or the
`PendingReview` itself changed between the preview and signing, the executor refuses as stale,
records a `failed` `capability.merge` row (reason named, no snapshots) and writes nothing: the
graph, the review included, is unchanged. The cases run the real reader, planner and executor over
a `CleanupRig`; only the stores are doubles.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from authz._fakes import FakeAccessRoleStore
from company_merge._kinds import UNREGISTERED_KIND

from graph_cleanup._fakes import ABSORBED, SURVIVOR
from graph_cleanup._gateway_fakes import POLICY, CleanupRig
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.executors import TOOL_MERGE_CAPABILITIES, execute_capability_merge
from ps_service.graph_cleanup.graph_reader import read_capability_merge_state
from ps_service.graph_cleanup.merge_planner import plan_capability_merge
from ps_service.logging import configure
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from collections.abc import Callable

_REVIEW = "review_aaa"
_OWNER = ("owner", "https://issuer.example.com/")
_OFFICER = ("officer", "https://issuer.example.com/")
_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=False,
)
_STALE_TEXT = "the graph changed since the preview"


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _row(rig: CleanupRig) -> PendingApprovalRow:
    state = read_capability_merge_state(rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED)
    now = datetime.now(UTC)
    return PendingApprovalRow(
        id="approval-1",
        code_hash=b"h",
        tool_name=TOOL_MERGE_CAPABILITIES,
        normalized_args={
            "survivor_id": SURVIVOR,
            "absorbed_id": ABSORBED,
            "acknowledge_governance_change": False,
            "state_digest": plan_capability_merge(state).state_digest,
            "review_id": _REVIEW,
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


def _world(rig: CleanupRig) -> PendingApprovalRow:
    rig.seed_capability_merge_world()
    rig.seed_near_miss_review(_REVIEW, incoming_id=ABSORBED, existing_id=SURVIVOR)
    return _row(rig)


def _execute(rig: CleanupRig, row: PendingApprovalRow) -> dict[str, object]:
    roles = FakeAccessRoleStore(expected_owner=_OWNER)
    roles.bootstrap_first_owner(_OWNER)
    roles.grant(actor=_OWNER, target=_OFFICER, access_role=AccessRole.COMPLIANCE_OFFICER)
    return execute_capability_merge(row, _CONFIG, rig.dependencies(roles))


def _graph_state(rig: CleanupRig) -> object:
    return copy.deepcopy(dict(rig.graph.nodes)), set(rig.graph.edges)


def _remove_absorbed(rig: CleanupRig) -> None:
    del rig.graph.nodes[("Capability", ABSORBED)]


def _deprecate_absorbed(rig: CleanupRig) -> None:
    rig.graph.nodes[("Capability", ABSORBED)]["status"] = "deprecated"


def _add_requirer(rig: CleanupRig) -> None:
    rig.seed_endpoint("Obligation", "obl_new")
    rig.seed_edge(("REQUIRES", "Obligation", "obl_new", "Capability", ABSORBED), legacy=False)


def _govern_survivor(rig: CleanupRig) -> None:
    rig.seed_policy("pol_new", title="New Policy", status="draft")
    rig.seed_governs(SURVIVOR, "pol_new")


def _delete_review(rig: CleanupRig) -> None:
    del rig.graph.nodes[("PendingReview", _REVIEW)]


def _repoint_review(rig: CleanupRig) -> None:
    rig.seed_endpoint("Capability", "cap_other_pair", name="other", status="active")
    rig.graph.nodes[("PendingReview", _REVIEW)]["nearest_existing_id"] = "cap_other_pair"


def _turn_review_into_a_review_of_an_unregistered_kind(rig: CleanupRig) -> None:
    rig.graph.nodes[("PendingReview", _REVIEW)]["kind"] = UNREGISTERED_KIND


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        pytest.param(_remove_absorbed, "revalidation_rejected", id="capability-gone"),
        pytest.param(_deprecate_absorbed, "revalidation_rejected", id="status-changed"),
        pytest.param(_add_requirer, "state_digest_mismatch", id="edge-count-changed"),
        pytest.param(_govern_survivor, "acknowledgment_missing", id="governing-policy-appeared"),
        pytest.param(_delete_review, "review_missing_or_changed", id="review-deleted"),
        pytest.param(_repoint_review, "review_missing_or_changed", id="review-repointed"),
        pytest.param(
            _turn_review_into_a_review_of_an_unregistered_kind,
            "review_missing_or_changed",
            id="kind",
        ),
    ],
)
def test_a_change_since_the_preview_is_refused_with_a_failed_row_and_nothing_written(
    cleanup_rig: CleanupRig, change: Callable[[CleanupRig], None], reason: str
) -> None:
    rig = cleanup_rig
    row = _world(rig)
    change(rig)
    before = _graph_state(rig)

    outcome = _execute(rig, row)

    assert "error" in outcome
    assert rig.logged_groups == []
    assert _graph_state(rig) == before
    [recorded] = rig.audit.rows
    assert (recorded.action, recorded.outcome, recorded.resource_id) == (
        "capability.merge",
        "failed",
        ABSORBED,
    )
    assert recorded.details["reason_code"] == reason
    assert recorded.details["review_id"] == _REVIEW
    assert recorded.details["approval_id"] == "approval-1"
    assert "before" not in recorded.details


@pytest.mark.parametrize(
    "change", [_delete_review, _repoint_review, _turn_review_into_a_review_of_an_unregistered_kind]
)
def test_a_review_that_is_gone_or_repointed_is_reported_as_stale(
    cleanup_rig: CleanupRig, change: Callable[[CleanupRig], None]
) -> None:
    row = _world(cleanup_rig)
    change(cleanup_rig)

    outcome = _execute(cleanup_rig, row)

    assert _STALE_TEXT in str(outcome["error"])
    assert ("Capability", ABSORBED) in cleanup_rig.graph.nodes
    assert cleanup_rig.graph.nodes[("Capability", ABSORBED)].get("status") != "deprecated"


def test_a_review_naming_the_pair_in_the_other_order_is_still_the_same_review(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    row = _world(rig)
    rig.graph.nodes[("PendingReview", _REVIEW)].update(
        incoming_id=SURVIVOR, nearest_existing_id=ABSORBED
    )

    outcome = _execute(rig, row)

    assert outcome == {"survivor_id": SURVIVOR, "absorbed_id": ABSORBED, "merged": True}
    assert ("PendingReview", _REVIEW) not in rig.graph.nodes


def test_a_merge_without_a_review_id_does_not_look_for_a_review(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    rig.seed_capability_merge_world()
    row = _row(rig)
    args = {k: v for k, v in row.normalized_args.items() if k != "review_id"}

    outcome = _execute(rig, replace(row, normalized_args=args))

    assert outcome == {"survivor_id": SURVIVOR, "absorbed_id": ABSORBED, "merged": True}


def test_a_policy_status_change_since_the_preview_is_refused_with_a_failed_row(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    rig.seed_capability_merge_world(governed=((ABSORBED, POLICY, "approved"),))
    rig.seed_near_miss_review(_REVIEW, incoming_id=ABSORBED, existing_id=SURVIVOR)
    row = replace(
        _row(rig),
        normalized_args={**_row(rig).normalized_args, "acknowledge_governance_change": True},
    )
    rig.graph.nodes[("Policy", POLICY)]["status"] = "deprecated"
    before = _graph_state(rig)

    outcome = _execute(rig, row)

    assert _STALE_TEXT in str(outcome["error"])
    assert _graph_state(rig) == before
    [recorded] = rig.audit.rows
    assert recorded.outcome == "failed"
    assert recorded.details["acknowledged"] is True
