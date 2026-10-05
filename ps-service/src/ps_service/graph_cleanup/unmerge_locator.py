"""Locating the merge an `unmerge` reverses, from the audit trail (issue #190, AC-BI-019, M4).

Reads `audit_events` through `AuditStore.query` only. The merge to reverse is the newest
`applied` merge row for the id that was not followed by a `failed` row with the same
`approval_id` (an `applied` + later `failed` pair means no edit occurred). The audit row says
what the merge intended; the live graph is verified separately by the planner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from ps_service.audit.models import AuditQueryFilters
from ps_service.graph_cleanup.audit_actions import (
    CAPABILITY_MERGE_ACTION,
    OBLIGATION_MERGE_ACTION,
    CapabilityMergeDetails,
    ObligationMergeDetails,
)
from ps_service.graph_cleanup.errors import GraphCleanupValidationError

if TYPE_CHECKING:
    from ps_service.audit.store import AuditStore

__all__ = ["LocatedMerge", "locate_effective_merge"]

_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class LocatedMerge:
    """The merge audit row an unmerge reverses, with its typed details."""

    kind: Literal["capability", "obligation"]
    approval_id: str
    details: CapabilityMergeDetails | ObligationMergeDetails


def _parse(action: str, raw: dict[str, object]) -> LocatedMerge:
    try:
        if action == CAPABILITY_MERGE_ACTION:
            capability = CapabilityMergeDetails.model_validate(raw)
            return LocatedMerge("capability", capability.approval_id, capability)
        obligation = ObligationMergeDetails.model_validate(raw)
    except ValidationError as exc:
        message = "the merge's audit record could not be read, so it cannot be reversed"
        raise GraphCleanupValidationError(message) from exc
    return LocatedMerge("obligation", obligation.approval_id, obligation)


def locate_effective_merge(audit_store: AuditStore, *, merged_id: str) -> LocatedMerge:
    """Return the newest merge of `merged_id` that actually applied.

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
            if event.action not in {CAPABILITY_MERGE_ACTION, OBLIGATION_MERGE_ACTION}:
                continue
            approval_id = str(event.details.get("approval_id", ""))
            if event.outcome == "failed":
                failed_approvals.add(approval_id)
            elif event.outcome == "applied" and approval_id not in failed_approvals:
                return _parse(event.action, event.details)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    message = (
        f"no merge of {merged_id!r} was found in the audit trail; only a merge made with "
        "merge-capabilities or merge-obligations can be unmerged"
    )
    raise GraphCleanupValidationError(message)
