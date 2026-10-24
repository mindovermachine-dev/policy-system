"""Locate the merge an `unmerge-entities` reverses, from the audit trail.

Reads `audit_events` through `AuditStore.query` only. The merge to reverse is the newest
`applied` merge row for the id that was not followed by a `failed` row with the same
`approval_id` (an `applied` + later `failed` pair means no edit occurred). The audit row says
what the merge intended; the live graph is verified separately by the planner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from pydantic import ValidationError

from ps_service.audit.models import AuditQueryFilters
from ps_service.graph_cleanup.audit_actions import (
    CAPABILITY_MERGE_ACTION,
    OBLIGATION_MERGE_ACTION,
    ROLE_MERGE_ACTION,
    CapabilityMergeDetails,
    ObligationMergeDetails,
    RoleMergeDetails,
)
from ps_service.graph_cleanup.errors import GraphCleanupValidationError

if TYPE_CHECKING:
    from datetime import datetime

    from ps_service.audit.models import AuditDetails, AuditEventRow
    from ps_service.audit.store import AuditStore

__all__ = ["LocatedMerge", "locate_effective_merge"]

_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class LocatedMerge:
    """The merge audit row an unmerge reverses, with its typed details.

    Attributes:
        kind: `"capability"`, `"obligation"` or `"role"`, matching the type of `details`.
        approval_id: Id of the approval that authorised the merge; ties the row to its
            `failed` follow-up, if any.
        event_id: Id of the merge's `applied` audit row.
        occurred_at: When that row was written; later rows are the ones that can depend on it.
        details: The audit row's `details` payload parsed into its typed model.
    """

    kind: Literal["capability", "obligation", "role"]
    approval_id: str
    event_id: str
    occurred_at: datetime
    details: CapabilityMergeDetails | ObligationMergeDetails | RoleMergeDetails


# The merge action of each kind and the model its `details` parse into.
_MERGE_KINDS: dict[str, tuple[Literal["capability", "obligation", "role"], type[AuditDetails]]] = {
    CAPABILITY_MERGE_ACTION: ("capability", CapabilityMergeDetails),
    OBLIGATION_MERGE_ACTION: ("obligation", ObligationMergeDetails),
    ROLE_MERGE_ACTION: ("role", RoleMergeDetails),
}


def _parse(event: AuditEventRow) -> LocatedMerge:
    """Parse an audit row's details into the typed model for its merge action."""
    kind, model = _MERGE_KINDS[event.action]
    try:
        details = model.model_validate(event.details)
    except ValidationError as exc:
        message = "the merge's audit record could not be read, so it cannot be reversed"
        raise GraphCleanupValidationError(message) from exc
    approval_id = str(event.details.get("approval_id", ""))
    return LocatedMerge(
        kind,
        approval_id,
        event.id,
        event.occurred_at,
        cast("CapabilityMergeDetails | ObligationMergeDetails | RoleMergeDetails", details),
    )


def locate_effective_merge(audit_store: AuditStore, *, merged_id: str) -> LocatedMerge:
    """Return the newest merge of `merged_id` that actually applied.

    Pages through the audit events for the resource newest-first; a `failed` row marks its
    `approval_id` as not applied, so an `applied` row with that approval id is skipped.

    Args:
        audit_store: Audit trail to search.
        merged_id: Id of the merged Capability, Obligation or Role; matched against the audit
            row's `resource_id`.

    Returns:
        The located merge with its parsed details. It is never `None`: absence raises.

    Raises:
        GraphCleanupValidationError: no such merge row exists, or its details are unreadable.
        AuditPostgresUnavailableError / AuditInvalidCursorError: the audit trail could not be read.
    """
    failed_approvals: set[str] = set()
    cursor: str | None = None
    filters = AuditQueryFilters(resource_id=merged_id)
    while True:
        page = audit_store.query(filters=filters, cursor=cursor, page_size=_PAGE_SIZE)
        for event in page.events:
            if event.action not in _MERGE_KINDS:
                continue
            approval_id = str(event.details.get("approval_id", ""))
            if event.outcome == "failed":
                failed_approvals.add(approval_id)
            elif event.outcome == "applied" and approval_id not in failed_approvals:
                return _parse(event)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    message = (
        f"no merge of {merged_id!r} was found in the audit trail; only a merge made with "
        "merge-capabilities, merge-obligations or merge-roles can be unmerged"
    )
    raise GraphCleanupValidationError(message)
