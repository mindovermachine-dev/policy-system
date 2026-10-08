"""Audit emission of `run_resolve_near_miss` (issue #195, AC-BI-012/011/010/001).

Hand-written structural fakes (no `unittest.mock`): a recording `InMemoryAuditStore` whose ordered
`events` list is shared with the fake graph so audit-before-effect is asserted on real ordering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest

from api._audit_fakes import InMemoryAuditStore
from ps_service.api.errors import PendingReviewNotFoundError
from ps_service.api.near_miss_review_orchestration import (
    NearMissReviewDependencies,
    run_resolve_near_miss,
)
from ps_service.audit import AuditContext, AuditPostgresUnavailableError, AuditTrailUnavailableError
from ps_service.company_merge.errors import CompanyMergePersistenceError, StalePendingReviewError
from ps_service.company_merge.models import PendingReviewRecord, ResolveOutcome
from ps_service.config import ServiceConfig

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.company_merge.falkordb_client import GraphHandle, GraphQueryResult

Decision = Literal["keep-separate", "merge"]

_ACTOR = ("actor-sub", "https://issuer.example.com/")
_RECORD = PendingReviewRecord(
    id="review_aaa",
    kind="Capability",
    incoming_id="capability_incoming_a",
    incoming_text="Report the incident.",
    nearest_existing_id="capability_existing_a",
    nearest_existing_text="Assess risk.",
    similarity=0.62,
    created_at="2026-01-01T00:00:00+00:00",
)


class _Graph:
    """Graph handle; never queried by the fakes below."""

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        raise NotImplementedError


def _config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        is_local_test_bypass_active=True,
    )


def _deps(
    store: InMemoryAuditStore,
    *,
    record: PendingReviewRecord | None = _RECORD,
    resolve: Callable[[str, Decision], ResolveOutcome | None] | None = None,
) -> NearMissReviewDependencies:
    def _resolve(_graph: GraphHandle, review_id: str, decision: Decision) -> ResolveOutcome | None:
        store.events.append("graph:resolve")
        if resolve is not None:
            return resolve(review_id, decision)
        return ResolveOutcome(review_id=review_id, decision=decision)

    return NearMissReviewDependencies(
        open_single_tenant_graph=lambda _config: _Graph(),
        list_pending_reviews=lambda _graph: (),
        resolve_review=_resolve,
        get_pending_review=lambda _graph, _review_id: record,
    )


def _run(
    store: InMemoryAuditStore,
    deps: NearMissReviewDependencies,
    decision: Decision = "keep-separate",
    *,
    approval_id: str | None = None,
) -> object:
    return run_resolve_near_miss(
        "review_aaa",
        decision,
        config=_config(),
        dependencies=deps,
        audit=AuditContext(_ACTOR, store),
        approval_id=approval_id,
    )


def test_resolve_keep_separate_records_applied_row_with_both_entity_ids_before_deleting_the_review() -> (  # noqa: E501
    None
):
    store = InMemoryAuditStore()

    _run(store, _deps(store))

    assert store.events == ["audit:near_miss.resolve:applied", "graph:resolve"]
    (row,) = store.rows
    assert (row.actor_subject, row.actor_issuer) == _ACTOR
    assert (row.action, row.resource_type, row.outcome) == (
        "near_miss.resolve",
        "near_miss_review",
        "applied",
    )
    assert row.details == {
        "review_id": "review_aaa",
        "kind": "Capability",
        "incoming_id": "capability_incoming_a",
        "existing_id": "capability_existing_a",
        "decision": "keep_separate",
    }


def test_resolve_keep_separate_resource_id_is_the_review_id() -> None:
    store = InMemoryAuditStore()

    _run(store, _deps(store))

    assert store.rows[0].resource_id == "review_aaa"


def test_resolve_merge_row_carries_the_approval_id() -> None:
    store = InMemoryAuditStore()

    _run(store, _deps(store), "merge", approval_id="approval-1")

    (row,) = store.rows
    assert row.details["decision"] == "merge"
    assert row.details["approval_id"] == "approval-1"


def test_resolve_records_failed_row_with_graph_write_failed_when_the_delete_raises() -> None:
    store = InMemoryAuditStore()

    def _boom(_review_id: str, _decision: Decision) -> ResolveOutcome | None:
        raise CompanyMergePersistenceError("FalkorDB write failed: secret-host:6379")

    with pytest.raises(CompanyMergePersistenceError):
        _run(store, _deps(store, resolve=_boom))

    assert [(r.outcome, r.details.get("reason_code")) for r in store.rows] == [
        ("applied", None),
        ("failed", "graph_write_failed"),
    ]


def test_resolve_records_failed_row_with_review_not_found_when_review_vanishes_after_the_opening_row() -> (  # noqa: E501
    None
):
    store = InMemoryAuditStore()

    with pytest.raises(PendingReviewNotFoundError):
        _run(store, _deps(store, resolve=lambda _r, _d: None))

    assert [(r.outcome, r.details.get("reason_code")) for r in store.rows] == [
        ("applied", None),
        ("failed", "review_not_found"),
    ]


def test_resolve_records_failed_row_with_review_stale_for_a_stale_merge() -> None:
    store = InMemoryAuditStore()

    def _stale(_review_id: str, _decision: Decision) -> ResolveOutcome | None:
        raise StalePendingReviewError("stale")

    with pytest.raises(PendingReviewNotFoundError):
        _run(store, _deps(store, resolve=_stale), "merge", approval_id="a-1")

    assert store.rows[-1].outcome == "failed"
    assert store.rows[-1].details["reason_code"] == "review_stale"


def test_resolve_records_unexpected_error_for_an_unclassified_exception_and_reraises() -> None:
    store = InMemoryAuditStore()

    def _boom(_review_id: str, _decision: Decision) -> ResolveOutcome | None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _run(store, _deps(store, resolve=_boom))

    assert store.rows[-1].details["reason_code"] == "unexpected_error"


def test_resolve_does_not_touch_the_graph_when_the_opening_row_cannot_be_written() -> None:
    store = InMemoryAuditStore(fail_on_outcome={"applied": AuditPostgresUnavailableError("down")})

    with pytest.raises(AuditTrailUnavailableError):
        _run(store, _deps(store))

    assert store.events == []
    assert store.rows == []


def test_resolve_unknown_review_writes_no_row_and_raises_not_found() -> None:
    store = InMemoryAuditStore()

    with pytest.raises(PendingReviewNotFoundError):
        _run(store, _deps(store, record=None))

    assert store.rows == []
    assert store.events == []


def test_resolve_failed_row_has_reason_code_and_no_free_text_or_traceback() -> None:
    store = InMemoryAuditStore()

    def _boom(_review_id: str, _decision: Decision) -> ResolveOutcome | None:
        raise CompanyMergePersistenceError("FalkorDB write failed: 10.0.0.1")

    with pytest.raises(CompanyMergePersistenceError):
        _run(store, _deps(store, resolve=_boom))

    failed = store.rows[-1]
    assert set(failed.details) <= {
        "review_id",
        "kind",
        "incoming_id",
        "existing_id",
        "decision",
        "approval_id",
        "reason_code",
    }
    assert "10.0.0.1" not in str(failed.details)


def test_resolve_failure_is_still_raised_when_the_failed_row_cannot_be_written() -> None:
    store = InMemoryAuditStore(fail_on_outcome={"failed": AuditPostgresUnavailableError("down")})

    def _boom(_review_id: str, _decision: Decision) -> ResolveOutcome | None:
        raise CompanyMergePersistenceError("x")

    with pytest.raises(CompanyMergePersistenceError):
        _run(store, _deps(store, resolve=_boom))

    assert [r.outcome for r in store.rows] == ["applied"]
