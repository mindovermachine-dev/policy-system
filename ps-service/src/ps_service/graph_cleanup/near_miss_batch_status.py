"""The creator's status read of a near-miss merge batch approval.

Reports `pending`, `expired` or `signed` and, once signed, one result per review id from the
outcome the signing step stored. A signed batch whose outcome was never stored is `unsettled` per
merge until it is well past its expiry; then, once the graph has caught up with its log, each
merge is settled against the graph and the audit trail. No merge is ever run again here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from ps_service.audit.models import AuditQueryFilters
from ps_service.graph_cleanup.near_miss_batch_execution import (
    merge_row_for_item,
    results_from_outcome,
    signed_items,
)
from ps_service.graph_cleanup.near_miss_batch_models import (
    TOOL_MERGE_NEAR_MISSES,
    NearMissBatchStatus,
    NearMissMergeResult,
)
from ps_service.graph_cleanup.service import (
    RECONCILE_GRACE,
    graph_has_caught_up,
    interrupted_audit_row,
    live_status,
)
from ps_service.logging import emit_log_entry
from ps_service.passkey_signing.executors import resolve_effect_verifier

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ps_service.audit.store import AuditStore
    from ps_service.config import ServiceConfig
    from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
    from ps_service.passkey_signing.models import PendingApprovalRow
    from ps_service.passkey_signing.store import PendingApprovalStore

__all__ = ["check_near_miss_merge_batch"]

_COMPONENT: Final = "graph_cleanup"
_PAGE_SIZE: Final = 100
_NOT_CARRIED_OUT: Final = (
    "the approved merge did not take effect; if you still intend to proceed, ask for a new approval"
)


def _unsettled(items: Sequence[Mapping[str, object]]) -> tuple[NearMissMergeResult, ...]:
    """One result per signed item whose outcome is not known yet."""
    return tuple(
        NearMissMergeResult(
            review_id=str(item.get("review_id", "")),
            kind=str(item.get("kind", "")),
            state="unsettled",
        )
        for item in items
    )


def _has_failed_row(audit_store: AuditStore, item_row: PendingApprovalRow) -> bool:
    """Whether the audit trail already holds a `failed` merge row of this item and approval."""
    absorbed_id = str(item_row.normalized_args.get("absorbed_id", ""))
    filters = AuditQueryFilters(resource_id=absorbed_id)
    cursor: str | None = None
    while True:
        page = audit_store.query(filters=filters, cursor=cursor, page_size=_PAGE_SIZE)
        for event in page.events:
            if event.outcome == "failed" and event.details.get("approval_id") == item_row.id:
                return True
        if page.next_cursor is None:
            return False
        cursor = page.next_cursor


def _settle_item(
    item: Mapping[str, object],
    item_row: PendingApprovalRow,
    *,
    config: ServiceConfig,
    dependencies: GraphCleanupDependencies,
) -> NearMissMergeResult:
    """Decide from the graph whether one merge happened; record the missing `failed` row if not."""
    verifier = resolve_effect_verifier(item_row.tool_name)
    graph = dependencies.open_single_tenant_graph(config)
    review_id, kind = str(item.get("review_id", "")), str(item.get("kind", ""))
    if verifier is not None and verifier(item_row, graph):
        return NearMissMergeResult(
            review_id=review_id,
            kind=kind,
            state="merged",
            winner_id=str(item_row.normalized_args.get("survivor_id")),
            loser_id=str(item_row.normalized_args.get("absorbed_id")),
        )
    audit_store = dependencies.audit_store(config)
    if not _has_failed_row(audit_store, item_row):
        action, resource_type, resource_id, details = interrupted_audit_row(item_row)
        audit_store.record_standalone(
            actor_subject=item_row.actor_subject,
            actor_issuer=item_row.actor_issuer,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome="failed",
            details=details,
        )
    return NearMissMergeResult(
        review_id=review_id, kind=kind, state="failed", error=_NOT_CARRIED_OUT
    )


def _settle(
    row: PendingApprovalRow,
    *,
    store: PendingApprovalStore,
    config: ServiceConfig,
    dependencies: GraphCleanupDependencies,
) -> tuple[NearMissMergeResult, ...]:
    """Settle every merge of a signed batch that has no stored outcome.

    The outcome is stored only when every merge is settled; anything that raises leaves the row
    untouched so a later check retries, and a `failed` row written by an earlier try is found
    again and not written twice. A graph that has not caught up with its log defers the whole
    settling, because a lagging graph would show a committed merge as absent.
    """
    items = signed_items(row)
    if not graph_has_caught_up(row, config=config, dependencies=dependencies):
        return _unsettled(items)
    results: list[NearMissMergeResult] = []
    for item in items:
        item_row = merge_row_for_item(row, item)
        if item_row is None:
            results.append(
                NearMissMergeResult(
                    review_id=str(item.get("review_id", "")),
                    kind=str(item.get("kind", "")),
                    state="failed",
                    error=_NOT_CARRIED_OUT,
                )
            )
        else:
            results.append(_settle_item(item, item_row, config=config, dependencies=dependencies))
    merged = sum(1 for result in results if result.state == "merged")
    store.set_outcome(
        row.id,
        {
            "merges": [result.model_dump() for result in results],
            "merged_count": merged,
            "failed_count": len(results) - merged,
        },
    )
    emit_log_entry(
        component=_COMPONENT,
        action="reconcile_near_miss_merge_batch",
        outcome="succeeded",
        extra={
            "approval_id": row.id,
            "merged_count": merged,
            "failed_count": len(results) - merged,
        },
    )
    return tuple(results)


def check_near_miss_merge_batch(
    *,
    pending_approval_id: str,
    actor: tuple[str, str] | None,
    store: PendingApprovalStore,
    config: ServiceConfig,
    dependencies: GraphCleanupDependencies,
) -> NearMissBatchStatus | None:
    """Return the caller's own batch approval and, once signed, the result of every merge.

    A signed batch with no stored outcome is `unsettled` per merge while it is inside the grace
    window or the graph is behind its log. Past `expires_at + RECONCILE_GRACE`, with the graph
    caught up, each merge is settled: present in the graph is `merged`; absent is `failed`, with an
    `interrupted_no_effect` audit row unless the trail already holds a `failed` row for it. The
    settled outcome is stored, so a repeat check changes nothing.

    Args:
        pending_approval_id: Id returned when the batch was created.
        actor: The caller's verified `(subject, issuer)`, or None when there is none.
        store: Pending-approval store; receives a settled outcome.
        config: Service configuration used to open the graph and the audit store.
        dependencies: Factories for the graph, audit store and gateway.

    Returns:
        The status. None for every case that must look like "not found": no resolvable actor, an
        unknown id, another actor's approval, or an approval that is not a batch approval.

    Raises:
        GraphCleanupPersistenceError: settling could not read the graph.
        AuditPostgresUnavailableError: settling could not read or write the audit trail (the batch
            is left unsettled).
        AuditPersistenceError: as above, for any other audit write failure.

    Side Effects:
        When settling, writes one `failed` audit row per merge absent from the graph that has none,
        stores the outcome on the approval row and emits log entries. Never runs a merge.
    """
    if actor is None:
        return None
    row = store.get_by_id(pending_approval_id)
    if row is None or (row.actor_subject, row.actor_issuer) != actor:
        return None
    if row.tool_name != TOOL_MERGE_NEAR_MISSES:
        return None
    if row.status != "signed":
        return NearMissBatchStatus(row.id, live_status(row), ())
    if row.outcome is not None:
        return NearMissBatchStatus(row.id, "signed", results_from_outcome(row.outcome))
    if datetime.now(UTC) > row.expires_at + RECONCILE_GRACE:
        merges = _settle(row, store=store, config=config, dependencies=dependencies)
    else:
        merges = _unsettled(signed_items(row))
    return NearMissBatchStatus(row.id, "signed", merges)
