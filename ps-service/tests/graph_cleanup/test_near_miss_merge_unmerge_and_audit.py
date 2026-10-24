"""A near-miss merge is audited like any cleanup merge and can be unmerged.

The real batch request, signing hand-off and batch executor run over a `CleanupRig`; the audit tests
read the `applied` row the executor recorded and the order of events around the gateway write. The
unmerge test plans the unmerge from that same row (the audit trail is the source of the
snapshots), signs and executes it, and looks at the graph afterwards.

An unmerge restores the loser and its edges; it does NOT recreate the `PendingReview` the merge
deleted. The pair is not filed again until a later ingest scores it below the threshold again.
That is the documented behaviour (a deleted review is not part of the merge snapshot) and one test
pins it.

`test_the_audit_chain_stays_intact_after_a_near_miss_merge` is `postgres_live`: it replays the
recorded row through the real audit store and verifies the hash chain with the independent oracle
and `verify_integrity`. It is deselected by default and unverified where no Postgres is reachable.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from audit._integrity_support import assert_chain_is_intact
from persistence.provisioned_postgres import migrate_state_database, provision_graph_log

from graph_cleanup._fakes import ABSORBED, SURVIVOR, FakeApprovalStore
from graph_cleanup._merge_runs import CONFIG, OFFICER, roles
from ps_service.audit import PsycopgAuditStore
from ps_service.audit.models import AuditEventRow
from ps_service.graph_cleanup.audit_actions import CapabilityMergeDetails
from ps_service.graph_cleanup.executors import execute_unmerge
from ps_service.graph_cleanup.near_miss_batch import create_near_miss_merge_batch
from ps_service.graph_cleanup.near_miss_batch_execution import execute_near_miss_batch
from ps_service.graph_cleanup.service import create_unmerge_approval

if TYPE_CHECKING:
    from persistence.provisioned_postgres import Provisioned

    from graph_cleanup._gateway_fakes import CleanupRig

_REVIEW = "review_aaa"
_BASE_URL = "https://ps.example.com"


def _world(rig: CleanupRig) -> None:
    rig.seed_capability_merge_world()
    rig.seed_near_miss_review(
        _REVIEW,
        incoming_id=ABSORBED,
        existing_id=SURVIVOR,
        incoming_created_at="2024-01-01T00:00:00+00:00",
        existing_created_at="2020-01-01T00:00:00+00:00",
    )


def _sign(store: FakeApprovalStore, approval_id: str):
    assert store.mark_signed(approval_id)
    row = store.get_by_id(approval_id)
    assert row is not None
    return row


def _merge_the_near_miss(rig: CleanupRig) -> dict[str, object]:
    store = FakeApprovalStore()

    approval = create_near_miss_merge_batch(
        rig.read,
        review_ids=[_REVIEW],
        acknowledged_review_ids=[],
        actor=OFFICER,
        base_url=_BASE_URL,
        store=store,
    )
    return execute_near_miss_batch(
        _sign(store, approval.pending_approval_id), CONFIG, rig.dependencies(roles())
    )


def _edges(rig: CleanupRig) -> set[tuple[str, str, str]]:
    return {(key[0], key[2], key[4]) for key in rig.graph.edges}


def test_the_applied_row_precedes_the_write_and_names_the_actor_pair_review_and_snapshots(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)

    outcome = _merge_the_near_miss(rig)

    assert outcome["merged_count"] == 1
    assert rig.order() == ["audit:applied", "log_append"]
    [recorded] = rig.audit.rows
    assert recorded.action == "capability.merge"
    assert recorded.outcome == "applied"
    assert (recorded.actor_subject, recorded.actor_issuer) == OFFICER
    assert recorded.resource_id == ABSORBED
    details = CapabilityMergeDetails.model_validate(recorded.details)
    assert (details.survivor_id, details.absorbed_id, details.review_id) == (
        SURVIVOR,
        ABSORBED,
        _REVIEW,
    )
    assert details.before is not None
    assert details.after is not None
    assert details.before != details.after


def test_unmerging_a_near_miss_merge_restores_the_loser_and_its_edges_but_not_the_review(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    edges_before = _edges(rig)
    _merge_the_near_miss(rig)
    [applied] = rig.audit.rows
    rig.audit.history = [
        AuditEventRow(
            id="evt-merge",
            occurred_at=datetime(2026, 10, 5, tzinfo=UTC),
            actor_subject=applied.actor_subject,
            actor_issuer=applied.actor_issuer,
            action=applied.action,
            resource_type=applied.resource_type,
            resource_id=applied.resource_id,
            outcome="applied",
            details=copy.deepcopy(applied.details),
        )
    ]
    store = FakeApprovalStore()

    approval = create_unmerge_approval(
        rig.read,
        rig.audit,
        merged_id=ABSORBED,
        actor=OFFICER,
        base_url=_BASE_URL,
        store=store,
    )
    outcome = execute_unmerge(
        _sign(store, approval.pending_approval_id), CONFIG, rig.dependencies(roles())
    )

    assert outcome["unmerged"] is True
    assert rig.graph.nodes[("Capability", ABSORBED)]["status"] == "active"
    assert not [key for key in rig.graph.edges if key[0] == "MERGED_INTO"]
    assert _edges(rig) == edges_before
    # The merge deleted the review and the unmerge does not recreate it: the pair is simply no
    # longer a pending near miss.
    assert ("PendingReview", _REVIEW) not in rig.graph.nodes


@pytest.mark.postgres_live
def test_the_audit_chain_stays_intact_after_a_near_miss_merge(
    cleanup_rig: CleanupRig, fresh_provisioned: Provisioned
) -> None:
    """Replay the merge's `applied` row through the real store; the hash chain verifies."""
    rig = cleanup_rig
    _world(rig)
    _merge_the_near_miss(rig)
    [applied] = rig.audit.rows
    migrate_state_database(fresh_provisioned)
    provision_graph_log(fresh_provisioned)
    store = PsycopgAuditStore(fresh_provisioned.state_config())

    store.record_standalone(
        actor_subject=applied.actor_subject,
        actor_issuer=applied.actor_issuer,
        action=applied.action,
        resource_type=applied.resource_type,
        resource_id=applied.resource_id,
        outcome="applied",
        details=applied.details,
    )

    verification = store.verify_integrity()
    assert verification.intact is True
    assert verification.first_break is None
    assert assert_chain_is_intact(fresh_provisioned) == verification.rows_checked == 1
