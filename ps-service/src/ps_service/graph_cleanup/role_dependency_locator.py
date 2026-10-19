"""Find a later merge that depends on a Role merge, from the audit trail.

A Role merge cannot be unmerged once something was built on its result. The audit trail is
authoritative for that (as it is for locating the merge): a graph marker alone cannot say which
audit event to reverse first. A later merge depends on the Role merge when it

- is an `applied` `obligation.merge` whose survivor or absorbed Obligation is one the Role merge
  moved (the Obligation cannot go back to the recreated Role while it is merged elsewhere), or
- is an `applied` `role.merge` that absorbed the Role merge's survivor (the survivor is gone).

A merge whose approval has a `failed` row never took effect, and a merge reversed by an `applied`
unmerge row (`reverses_approval_id`) no longer stands; neither depends. Reads `audit_events`
through `AuditStore.query` only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ps_service.audit.models import AuditQueryFilters
from ps_service.graph_cleanup.audit_actions import (
    OBLIGATION_MERGE_ACTION,
    OBLIGATION_UNMERGE_ACTION,
    ROLE_MERGE_ACTION,
    ROLE_UNMERGE_ACTION,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.audit.models import AuditEventRow
    from ps_service.audit.store import AuditStore
    from ps_service.graph_cleanup.audit_actions import RoleMergeDetails
    from ps_service.graph_cleanup.unmerge_locator import LocatedMerge

__all__ = ["DependentMerge", "find_dependent_merge"]

_PAGE_SIZE = 100
_MERGE_ACTIONS = (OBLIGATION_MERGE_ACTION, ROLE_MERGE_ACTION)
_UNMERGE_ACTIONS = (OBLIGATION_UNMERGE_ACTION, ROLE_UNMERGE_ACTION)


@dataclass(frozen=True, slots=True)
class DependentMerge:
    """A later merge that stands on a Role merge's result.

    Attributes:
        event_id: Id of the dependent's `applied` audit row, the one to reverse first.
        approval_id: Id of the approval that authorised the dependent.
        action: `obligation.merge` or `role.merge`.
    """

    event_id: str
    approval_id: str
    action: str


def _rows_since(
    audit_store: AuditStore, action: str, merge: LocatedMerge
) -> Iterator[AuditEventRow]:
    """Every row of `action` written at or after the merge, newest first, across pages."""
    filters = AuditQueryFilters(action=action, occurred_from=merge.occurred_at)
    cursor: str | None = None
    while True:
        page = audit_store.query(filters=filters, cursor=cursor, page_size=_PAGE_SIZE)
        yield from page.events
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def _approval(event: AuditEventRow) -> str:
    return str(event.details.get("approval_id", ""))


def _depends(event: AuditEventRow, details: RoleMergeDetails, moved: frozenset[str]) -> bool:
    """Whether the `applied` merge `event` involves what the Role merge moved or left behind."""
    if event.action == ROLE_MERGE_ACTION:
        return event.details.get("absorbed_id") == details.survivor_id
    return bool({event.details.get("survivor_id"), event.details.get("absorbed_id")} & moved)


def find_dependent_merge(
    audit_store: AuditStore, merge: LocatedMerge, details: RoleMergeDetails
) -> DependentMerge | None:
    """Return the newest later merge that depends on the Role merge `merge`, or None.

    Args:
        audit_store: Audit trail to search.
        merge: The located `role.merge` (its event id and time bound the search).
        details: The Role merge's parsed details: its survivor and the Obligations it moved.

    Returns:
        The newest standing dependent, or None when nothing depends on the merge.

    Raises:
        AuditPostgresUnavailableError / AuditInvalidCursorError: the audit trail could not be read.
    """
    moved = frozenset(details.moved_obligation_ids or ())
    failed: set[str] = set()
    reversals: list[tuple[str, str]] = []
    merges: list[AuditEventRow] = []
    for action in (*_MERGE_ACTIONS, *_UNMERGE_ACTIONS):
        for event in _rows_since(audit_store, action, merge):
            if event.outcome == "failed":
                failed.add(_approval(event))
            elif event.outcome != "applied":
                continue
            elif action in _UNMERGE_ACTIONS:
                reversals.append((_approval(event), str(event.details.get("reverses_approval_id"))))
            elif event.id != merge.event_id:
                merges.append(event)
    reversed_approvals = {
        reversed_id for approval, reversed_id in reversals if approval not in failed
    }
    standing = [
        e
        for e in merges
        if _approval(e) not in failed
        and _approval(e) not in reversed_approvals
        and _depends(e, details, moved)
    ]
    if not standing:
        return None
    newest = max(standing, key=lambda e: (e.occurred_at, e.id))
    return DependentMerge(newest.id, _approval(newest), newest.action)
