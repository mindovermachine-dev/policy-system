"""A merge carried out by a near-miss batch is unmerged on its own, from its own audit rows.

A batch is created, signed and executed over a `CleanupRig`; the `applied` rows it wrote become the
audit history (newest first), and an unmerge is planned, signed and executed for one merged id.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast

import pytest
from audit._integrity_support import assert_chain_is_intact
from persistence.provisioned_postgres import migrate_state_database, provision_graph_log

from graph_cleanup._fakes import FakeApprovalStore
from graph_cleanup._merge_runs import CONFIG, OFFICER, roles
from graph_cleanup._near_miss_batch_worlds import (
    SeededPair,
    seed_capability_pair_with_review,
    seed_role_pair_with_review,
)
from ps_service.audit import PsycopgAuditStore
from ps_service.audit.models import AuditEventRow
from ps_service.graph_cleanup.executors import execute_unmerge
from ps_service.graph_cleanup.near_miss_batch import create_near_miss_merge_batch
from ps_service.graph_cleanup.near_miss_batch_execution import execute_near_miss_batch
from ps_service.graph_cleanup.service import create_unmerge_approval
from ps_service.logging import configure

if TYPE_CHECKING:
    from persistence.provisioned_postgres import Provisioned

    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.passkey_signing.models import PendingApprovalRow

_BASE_URL = "https://ps.example.com"
type Outcome = Literal["applied", "rejected", "failed"]


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _signed(store: FakeApprovalStore, approval_id: str) -> PendingApprovalRow:
    assert store.mark_signed(approval_id)
    row = store.get_by_id(approval_id)
    assert row is not None
    return row


def _run_batch(
    rig: CleanupRig, pairs: list[SeededPair], *, stale: SeededPair | None = None
) -> PendingApprovalRow:
    store = FakeApprovalStore()
    approval = create_near_miss_merge_batch(
        rig.read,
        review_ids=[pair.review_id for pair in pairs],
        acknowledged_review_ids=[],
        actor=OFFICER,
        base_url=_BASE_URL,
        store=store,
    )
    row = _signed(store, approval.pending_approval_id)
    if stale is not None:
        _stale(rig, stale)
    execute_near_miss_batch(row, CONFIG, rig.dependencies(roles()))
    return row


def _use_audit_rows_as_history(rig: CleanupRig) -> None:
    start = datetime(2026, 10, 5, tzinfo=UTC)
    rows = [
        AuditEventRow(
            id=f"evt-{index}",
            occurred_at=start + timedelta(seconds=index),
            actor_subject=row.actor_subject,
            actor_issuer=row.actor_issuer,
            action=row.action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            outcome=cast("Outcome", row.outcome),
            details=copy.deepcopy(row.details),
        )
        for index, row in enumerate(rig.audit.rows)
    ]
    rig.audit.history = list(reversed(rows))


def _unmerge(rig: CleanupRig, merged_id: str) -> dict[str, object]:
    _use_audit_rows_as_history(rig)
    store = FakeApprovalStore()
    approval = create_unmerge_approval(
        rig.read,
        rig.audit,
        merged_id=merged_id,
        actor=OFFICER,
        base_url=_BASE_URL,
        store=store,
    )
    return execute_unmerge(
        _signed(store, approval.pending_approval_id), CONFIG, rig.dependencies(roles())
    )


def _stale(rig: CleanupRig, pair: SeededPair) -> None:
    rig.seed_endpoint("Obligation", f"obl_late_{pair.absorbed_id}")
    rig.seed_edge(
        ("REQUIRES", "Obligation", f"obl_late_{pair.absorbed_id}", "Capability", pair.absorbed_id),
        legacy=False,
    )


def test_unmerging_a_capability_merged_in_a_batch_restores_it_and_its_edges_but_not_the_review(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    pair = seed_capability_pair_with_review(rig, 1)
    edges_before = {(key[0], key[2], key[4]) for key in rig.graph.edges}
    _run_batch(rig, [pair])

    outcome = _unmerge(rig, pair.absorbed_id)

    assert outcome["unmerged"] is True
    assert rig.graph.nodes[("Capability", pair.absorbed_id)]["status"] == "active"
    assert not [key for key in rig.graph.edges if key[0] == "MERGED_INTO"]
    assert {(key[0], key[2], key[4]) for key in rig.graph.edges} == edges_before
    assert ("PendingReview", pair.review_id) not in rig.graph.nodes


def test_unmerging_a_role_merged_in_a_batch_restores_it_and_its_obligations(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    pair = seed_role_pair_with_review(rig, 1)
    _run_batch(rig, [pair])

    outcome = _unmerge(rig, pair.absorbed_id)

    assert outcome["unmerged"] is True
    assert ("Role", pair.absorbed_id) in rig.graph.nodes
    assert ("MergedRole", pair.absorbed_id) not in rig.graph.nodes


def test_unmerge_of_one_item_leaves_the_other_merged_items_merged(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    first = seed_capability_pair_with_review(rig, 1)
    second = seed_capability_pair_with_review(rig, 2)
    role = seed_role_pair_with_review(rig, 3)
    _run_batch(rig, [first, second, role])

    outcome = _unmerge(rig, first.absorbed_id)

    assert outcome["unmerged"] is True
    assert ("Capability", first.absorbed_id) in rig.graph.nodes
    assert ("Capability", second.absorbed_id) not in rig.graph.nodes
    assert rig.graph.nodes[("MergedCapability", second.absorbed_id)]["merged_into"] == (
        second.survivor_id
    )
    assert ("MergedRole", role.absorbed_id) in rig.graph.nodes


def test_unmerge_finds_the_merged_item_although_a_sibling_of_the_same_approval_failed(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    first = seed_capability_pair_with_review(rig, 1)
    second = seed_capability_pair_with_review(rig, 2)
    _run_batch(rig, [first, second], stale=second)
    assert {row.outcome for row in rig.audit.rows} == {"applied", "failed"}

    outcome = _unmerge(rig, first.absorbed_id)

    assert outcome["unmerged"] is True
    assert ("Capability", first.absorbed_id) in rig.graph.nodes
    assert ("Capability", second.absorbed_id) in rig.graph.nodes


def test_the_unmerge_preview_names_the_batch_approval_as_the_merge_it_reverses(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    first = seed_capability_pair_with_review(rig, 1)
    second = seed_capability_pair_with_review(rig, 2)
    batch = _run_batch(rig, [first, second])
    _use_audit_rows_as_history(rig)

    approval = create_unmerge_approval(
        rig.read,
        rig.audit,
        merged_id=second.absorbed_id,
        actor=OFFICER,
        base_url=_BASE_URL,
        store=FakeApprovalStore(),
    )

    preview = cast("dict[str, object]", approval.preview.model_dump())
    assert preview["merge_approval_id"] == batch.id
    assert preview["merged_id"] == second.absorbed_id
    assert preview["survivor_id"] == second.survivor_id


@pytest.mark.postgres_live
def test_the_audit_chain_stays_intact_after_a_batch(
    cleanup_rig: CleanupRig, fresh_provisioned: Provisioned
) -> None:
    """Replay the rows a batch wrote through the real store; the hash chain verifies."""
    rig = cleanup_rig
    first = seed_capability_pair_with_review(rig, 1)
    second = seed_role_pair_with_review(rig, 2)
    _run_batch(rig, [first, second])
    migrate_state_database(fresh_provisioned)
    provision_graph_log(fresh_provisioned)
    store = PsycopgAuditStore(fresh_provisioned.state_config())

    for row in rig.audit.rows:
        store.record_standalone(
            actor_subject=row.actor_subject,
            actor_issuer=row.actor_issuer,
            action=row.action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            outcome=cast("Outcome", row.outcome),
            details=row.details,
        )

    verification = store.verify_integrity()
    assert verification.intact is True
    assert assert_chain_is_intact(fresh_provisioned) == verification.rows_checked == 2
