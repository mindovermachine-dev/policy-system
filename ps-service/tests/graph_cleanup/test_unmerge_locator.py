"""Locating the merge an `unmerge` reverses, from the audit trail (issue #190, slice 15 c; M4).

The newest `applied` merge row for the id that was not followed by a `failed` row with the same
`approval_id`. `AuditStore.query` is the only read; live graph state is verified by the planner.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from graph_cleanup._fakes import ABSORBED, SURVIVOR, RecordingAuditStore
from ps_service.audit.errors import AuditPostgresUnavailableError
from ps_service.audit.models import AuditEventRow
from ps_service.graph_cleanup.audit_actions import CapabilityMergeDetails
from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.unmerge_locator import locate_effective_merge

_BASE = datetime(2026, 10, 5, tzinfo=UTC)


def _event(
    n: int,
    *,
    action: str = "capability.merge",
    outcome: Literal["applied", "failed"] = "applied",
    approval_id: str = "ap-1",
    resource_id: str = ABSORBED,
    details: dict[str, object] | None = None,
) -> AuditEventRow:
    body: dict[str, object] = details or {
        "survivor_id": SURVIVOR,
        "absorbed_id": resource_id,
        "policy_case": 1,
        "acknowledged": False,
        "approval_id": approval_id,
        "before": {"nodes": [], "edges": []},
        "after": {"nodes": [], "edges": []},
    }
    return AuditEventRow(
        id=f"evt-{n}",
        occurred_at=_BASE + timedelta(minutes=n),
        actor_subject="officer",
        actor_issuer="https://issuer.example.com/",
        action=action,
        resource_type="capability",
        resource_id=resource_id,
        outcome=outcome,
        details=body,
    )


def _store(*events: AuditEventRow) -> RecordingAuditStore:
    """Events given oldest first, stored newest first like the real store returns them."""
    return RecordingAuditStore(history=list(reversed(events)))


def test_returns_the_applied_capability_merge_with_its_typed_details() -> None:
    located = locate_effective_merge(_store(_event(1)), merged_id=ABSORBED)

    assert located.kind == "capability"
    assert located.approval_id == "ap-1"
    assert isinstance(located.details, CapabilityMergeDetails)
    assert located.details.survivor_id == SURVIVOR


def test_an_applied_row_with_a_later_failed_row_for_the_same_approval_is_not_a_merge() -> None:
    store = _store(
        _event(1, approval_id="ap-old"),
        _event(2, approval_id="ap-new"),
        _event(3, outcome="failed", approval_id="ap-new"),
    )

    located = locate_effective_merge(store, merged_id=ABSORBED)

    assert located.approval_id == "ap-old"


def test_the_newest_effective_merge_wins() -> None:
    store = _store(_event(1, approval_id="ap-1"), _event(2, approval_id="ap-2"))

    assert locate_effective_merge(store, merged_id=ABSORBED).approval_id == "ap-2"


def test_other_actions_for_the_same_resource_are_ignored() -> None:
    store = _store(
        _event(1, approval_id="ap-1"),
        _event(2, action="capability.release_governance", approval_id="ap-rel"),
        _event(
            3,
            action="capability.unmerge",
            approval_id="ap-un",
            details={
                "survivor_id": SURVIVOR,
                "absorbed_id": ABSORBED,
                "approval_id": "ap-un",
                "reverses_approval_id": "ap-1",
            },
        ),
    )

    assert locate_effective_merge(store, merged_id=ABSORBED).approval_id == "ap-1"


def test_no_merge_row_is_an_explained_error() -> None:
    with pytest.raises(GraphCleanupValidationError, match="no merge"):
        locate_effective_merge(_store(), merged_id=ABSORBED)


def test_only_failed_merge_rows_is_an_explained_error() -> None:
    store = _store(_event(1), _event(2, outcome="failed"))

    with pytest.raises(GraphCleanupValidationError, match="no merge"):
        locate_effective_merge(store, merged_id=ABSORBED)


def test_it_pages_through_the_trail() -> None:
    noise = [
        _event(n, action="capability.release_governance", approval_id=f"r-{n}")
        for n in range(2, 260)
    ]
    store = _store(_event(1, approval_id="ap-deep"), *noise)

    located = locate_effective_merge(store, merged_id=ABSORBED)

    assert located.approval_id == "ap-deep"
    assert store.query_calls > 1


def test_an_unreadable_audit_trail_propagates_the_store_error() -> None:
    store = RecordingAuditStore(query_error=AuditPostgresUnavailableError())

    with pytest.raises(AuditPostgresUnavailableError):
        locate_effective_merge(store, merged_id=ABSORBED)


def test_a_merge_row_that_does_not_match_its_model_is_an_explained_error() -> None:
    store = _store(_event(1, details={"approval_id": "ap-1", "junk": True}))

    with pytest.raises(GraphCleanupValidationError, match="audit record"):
        locate_effective_merge(store, merged_id=ABSORBED)


def test_an_obligation_merge_row_is_located_as_an_obligation_with_its_typed_details() -> None:
    from ps_service.graph_cleanup.audit_actions import ObligationMergeDetails

    store = _store(
        _event(
            1,
            action="obligation.merge",
            resource_id="obl_a",
            approval_id="ap-obl",
            details={
                "survivor_id": "obl_s",
                "absorbed_id": "obl_a",
                "role_id": "role_1",
                "approval_id": "ap-obl",
                "before": {"nodes": [], "edges": []},
                "after": {"nodes": [], "edges": []},
            },
        )
    )

    located = locate_effective_merge(store, merged_id="obl_a")

    assert located.kind == "obligation"
    assert isinstance(located.details, ObligationMergeDetails)
    assert located.details.role_id == "role_1"
