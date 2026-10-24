"""Execute a signed near-miss merge batch: every item runs through its kind's cleanup merge.

The approval executor of `merge-near-misses`. Each signed item becomes the argument set of the
`merge-capabilities` or `merge-roles` approval it stands for and runs through that kind's executor,
which re-checks the approver's role, re-plans the merge against the current graph, compares the
state digest, writes the audit row first and then one gateway group that also deletes the
`PendingReview`. This module holds no merge logic of its own.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Final, cast

from ps_service.graph_cleanup.dependencies import build_default_graph_cleanup_dependencies
from ps_service.graph_cleanup.executors import (
    TOOL_MERGE_CAPABILITIES,
    TOOL_MERGE_ROLES,
    execute_capability_merge,
    execute_role_merge,
)
from ps_service.graph_cleanup.near_miss_batch_models import (
    TOOL_MERGE_NEAR_MISSES,
    NearMissMergeResult,
)
from ps_service.graph_cleanup.service import interrupted_audit_row
from ps_service.logging import emit_log_entry
from ps_service.passkey_signing.executors import register_approval_executor

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ps_service.config import ServiceConfig
    from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
    from ps_service.passkey_signing.models import PendingApprovalRow

__all__ = ["execute_near_miss_batch", "merge_row_for_item", "results_from_outcome", "signed_items"]

_COMPONENT: Final = "graph_cleanup"
_BAD_APPROVAL: Final = "this approval could not be executed; ask for a new approval"
_ITEM_FAILED: Final = "this merge could not be carried out because of an unexpected error"

type _ItemExecutor = Callable[
    [PendingApprovalRow, ServiceConfig, GraphCleanupDependencies], dict[str, object]
]

_EXECUTORS: Final[dict[str, tuple[str, _ItemExecutor]]] = {
    "Capability": (TOOL_MERGE_CAPABILITIES, execute_capability_merge),
    "Role": (TOOL_MERGE_ROLES, execute_role_merge),
}


def merge_row_for_item(
    row: PendingApprovalRow, item: Mapping[str, object]
) -> PendingApprovalRow | None:
    """The approval row of the single merge `item` stands for; `row.id` stays the shared id.

    Returns:
        The row, or None when the item's kind has no merge.
    """
    registered = _EXECUTORS.get(str(item.get("kind")))
    if registered is None:
        return None
    arguments = {
        name: item[name]
        for name in (
            "survivor_id",
            "absorbed_id",
            "state_digest",
            "review_id",
            "acknowledge_governance_change",
        )
        if name in item
    }
    return dataclasses.replace(row, tool_name=registered[0], normalized_args=arguments)


def _result(item: Mapping[str, object], outcome: Mapping[str, object]) -> NearMissMergeResult:
    """Map one per-kind executor outcome to the item's result."""
    review_id = str(item.get("review_id", ""))
    kind = str(item.get("kind", ""))
    survivor = outcome.get("survivor_id")
    absorbed = outcome.get("absorbed_id")
    if outcome.get("merged") is True and isinstance(survivor, str) and isinstance(absorbed, str):
        return NearMissMergeResult(
            review_id=review_id, kind=kind, state="merged", winner_id=survivor, loser_id=absorbed
        )
    error = outcome.get("error")
    return NearMissMergeResult(
        review_id=review_id,
        kind=kind,
        state="failed",
        error=error if isinstance(error, str) else _BAD_APPROVAL,
    )


def _log_item_failure(item_row: PendingApprovalRow, reason: str) -> None:
    """Log that one merge of a batch failed unexpectedly; `reason` carries no exception text."""
    emit_log_entry(
        component=_COMPONENT,
        action="execute_near_miss_batch_item",
        outcome="failed",
        extra={
            "approval_id": item_row.id,
            "review_id": str(item_row.normalized_args.get("review_id", "")),
            "reason": reason,
        },
    )


def _record_item_failed_row(
    item_row: PendingApprovalRow, config: ServiceConfig, dependencies: GraphCleanupDependencies
) -> None:
    """Best effort: record the `failed` merge row of an item that failed unexpectedly."""
    action, resource_type, resource_id, details = interrupted_audit_row(item_row)
    try:
        dependencies.audit_store(config).record_standalone(
            actor_subject=item_row.actor_subject,
            actor_issuer=item_row.actor_issuer,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome="failed",
            details=details,
        )
    except Exception as exc:  # noqa: BLE001 -- the audit trail may be the very thing that failed; the batch outcome must still be stored
        _log_item_failure(item_row, f"failed_row_not_recorded:{type(exc).__name__}")


def _run_item(
    row: PendingApprovalRow,
    item: Mapping[str, object],
    config: ServiceConfig,
    dependencies: GraphCleanupDependencies,
) -> NearMissMergeResult:
    """Run one signed item through the executor of its kind."""
    item_row = merge_row_for_item(row, item)
    if item_row is None:
        return _result(item, {"error": _BAD_APPROVAL})
    executor = _EXECUTORS[str(item["kind"])][1]
    try:
        return _result(item, executor(item_row, config, dependencies))
    except Exception as exc:  # noqa: BLE001 -- best effort: one merge's unexpected failure must not stop the others or lose the batch outcome; only the class name is logged
        _log_item_failure(item_row, type(exc).__name__)
        _record_item_failed_row(item_row, config, dependencies)
        return NearMissMergeResult(
            review_id=str(item.get("review_id", "")),
            kind=str(item.get("kind", "")),
            state="failed",
            error=_ITEM_FAILED,
        )


def signed_items(row: PendingApprovalRow) -> list[Mapping[str, object]]:
    """The merge items signed into a batch approval, in the order the officer chose them."""
    merges = row.normalized_args.get("merges")
    if not isinstance(merges, list):
        return []
    return [
        cast("Mapping[str, object]", item)
        for item in cast("list[object]", merges)
        if isinstance(item, dict)
    ]


def execute_near_miss_batch(
    row: PendingApprovalRow, config: ServiceConfig, dependencies: GraphCleanupDependencies
) -> dict[str, object]:
    """Execute one signed `merge-near-misses` approval; return its outcome dict.

    Args:
        row: The verified approval; its `normalized_args["merges"]` carry the signed items.
        config: Service configuration used to open the graph and the stores.
        dependencies: Factories for the graph, audit store, access-role store and gateway.

    Returns:
        `{"merges": [<one entry per signed item>], "merged_count", "failed_count"}`. An entry has
        `review_id`, `kind`, `state` (`merged` or `failed`), `winner_id`, `loser_id` and `error`.

        An item that fails, even unexpectedly, is a `failed` entry; the other items still run and no
        failed item is retried. This function does not raise.

    Side Effects:
        Whatever each item's cleanup merge does: an audit row, one gateway group and the deletion
        of the item's `PendingReview`. An item that failed unexpectedly gets a best-effort `failed`
        audit row. Emits structured log entries.
    """
    results = [_run_item(row, item, config, dependencies) for item in signed_items(row)]
    merged = sum(1 for result in results if result.state == "merged")
    emit_log_entry(
        component=_COMPONENT,
        action="execute_near_miss_batch",
        outcome="succeeded",
        extra={
            "approval_id": row.id,
            "merged_count": merged,
            "failed_count": len(results) - merged,
        },
    )
    return {
        "merges": [result.model_dump() for result in results],
        "merged_count": merged,
        "failed_count": len(results) - merged,
    }


def results_from_outcome(outcome: Mapping[str, object]) -> tuple[NearMissMergeResult, ...]:
    """The per-merge results a stored batch outcome holds; empty when it holds none."""
    merges = outcome.get("merges")
    if not isinstance(merges, list):
        return ()
    return tuple(NearMissMergeResult.model_validate(item) for item in cast("list[object]", merges))


def _execute_registered(row: PendingApprovalRow, config: ServiceConfig) -> dict[str, object]:
    return execute_near_miss_batch(row, config, build_default_graph_cleanup_dependencies())


register_approval_executor(TOOL_MERGE_NEAR_MISSES, _execute_registered)
