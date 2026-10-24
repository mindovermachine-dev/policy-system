"""Audit of a cleanup approval that another enrolled actor signed with a verified passkey.

The signing router consumes such an approval with a fixed error outcome, so it can never execute,
and then calls the recorder this module registers for every passkey-approved cleanup tool. The
recorder writes the `failed` row of the approval's own action (`capability.merge`,
`capability.split`, ...) with the verified signer as actor and `signer_not_bound_actor` as the
reason, built from the approval alone by `failed_audit_row`. A signed near-miss batch records one
row per signed merge, each under its kind's merge action. Nothing is written to the graph.

The module registers its recorder with `passkey_signing.executors` at import (same idiom as
`register_approval_executor`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from ps_service.graph_cleanup.dependencies import build_default_graph_cleanup_dependencies
from ps_service.graph_cleanup.executors import CLEANUP_TOOL_NAMES
from ps_service.graph_cleanup.near_miss_batch_execution import merge_row_for_item, signed_items
from ps_service.graph_cleanup.near_miss_batch_models import TOOL_MERGE_NEAR_MISSES
from ps_service.graph_cleanup.service import failed_audit_row
from ps_service.logging import emit_log_entry
from ps_service.passkey_signing.executors import register_signing_refusal_recorder

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig
    from ps_service.passkey_signing.executors import SignerIdentity
    from ps_service.passkey_signing.models import PendingApprovalRow

__all__ = ["record_signer_refusal"]

_COMPONENT: Final = "graph_cleanup"


def _refused_rows(row: PendingApprovalRow) -> list[PendingApprovalRow]:
    """The approval rows a refusal is recorded for: each signed merge of a batch, else the row."""
    if row.tool_name != TOOL_MERGE_NEAR_MISSES:
        return [row]
    item_rows = (merge_row_for_item(row, item) for item in signed_items(row))
    return [item_row for item_row in item_rows if item_row is not None]


def record_signer_refusal(
    row: PendingApprovalRow, config: ServiceConfig, signer: SignerIdentity
) -> None:
    """Record the `failed` row(s) of a cleanup approval another actor signed.

    Args:
        row: The voided approval; its tool and signed arguments name the action and resources.
        config: Service configuration used to open the audit store.
        signer: The verified owner of the passkey that signed the approval.

    Raises:
        AuditPostgresUnavailableError: the audit store could not be reached.
        AuditPersistenceError: any other audit write failure.

    Side Effects:
        Appends one `failed` audit row per action (`reason_code` `signer_not_bound_actor`, the
        approval's id as `approval_id`) and emits one log entry.
    """
    audit = build_default_graph_cleanup_dependencies().audit_store(config)
    for refused in _refused_rows(row):
        action, resource_type, resource_id, details = failed_audit_row(
            refused, "signer_not_bound_actor"
        )
        audit.record_standalone(
            actor_subject=signer.subject,
            actor_issuer=signer.issuer,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome="failed",
            details=details,
        )
    emit_log_entry(
        component=_COMPONENT,
        action="record_signer_refusal",
        outcome="succeeded",
        extra={"approval_id": row.id, "tool_name": row.tool_name},
    )


for _tool_name in sorted(CLEANUP_TOOL_NAMES | {TOOL_MERGE_NEAR_MISSES}):
    register_signing_refusal_recorder(_tool_name, record_signer_refusal)
