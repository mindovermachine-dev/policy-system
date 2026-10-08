"""The passkey-approved near-miss merge is audited as `near_miss.resolve` (issue #195, AC-BI-012).

The APPROVER is the actor, the `approval_id` is in the details, the `applied` row precedes the graph
merge and a failed merge adds a `failed` row. Creating the pending approval writes nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, cast

import pytest
from api.test_routes_near_misses import (
    _fake_dependencies,  # pyright: ignore[reportPrivateUsage]
    _record,  # pyright: ignore[reportPrivateUsage]
)
from audit._fakes import InMemoryAuditStore
from webauthn.helpers import base64url_to_bytes

from passkey_signing._fakes import FakePendingApprovalStore, FakeSigningCredentialStore
from passkey_signing.test_signing_ceremony import (
    _ACTOR_ISSUER,  # pyright: ignore[reportPrivateUsage]
    _ACTOR_SUBJECT,  # pyright: ignore[reportPrivateUsage]
    _ORIGIN,  # pyright: ignore[reportPrivateUsage]
    _REVIEW_ID,  # pyright: ignore[reportPrivateUsage]
    _RP_ID,  # pyright: ignore[reportPrivateUsage]
    _Authenticator,  # pyright: ignore[reportPrivateUsage]
    _client,  # pyright: ignore[reportPrivateUsage]
    _enroll,  # pyright: ignore[reportPrivateUsage]
    _seed_pending_approval,  # pyright: ignore[reportPrivateUsage]
)
from ps_service.api.dependencies import provide_near_miss_review_dependencies
from ps_service.audit import AuditPostgresUnavailableError
from ps_service.auth import Principal
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.falkordb_client import (
    GraphHandle,
)
from ps_service.company_merge.models import ResolveOutcome
from ps_service.logging import configure

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _configured_logging() -> None:  # pyright: ignore[reportUnusedFunction]  # autouse
    """The failure paths emit semantic log entries, which need a configured facade."""
    configure()


Resolve = Callable[[GraphHandle, str, Literal["keep-separate", "merge"]], ResolveOutcome | None]


def _merge_resolve(audit: InMemoryAuditStore) -> Resolve:
    def _resolve(
        graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveOutcome | None:
        _ = graph
        audit.events.append("graph:merge")
        return ResolveOutcome(
            review_id=review_id, decision=decision, winner_id="winner", loser_id="loser"
        )

    return _resolve


def _sign(
    *, audit: InMemoryAuditStore, resolve: Resolve | None = None
) -> tuple[dict[str, object], str]:
    """Run the full ceremony for one seeded merge approval; return the sign/verify body + row id."""
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    authenticator = _Authenticator()
    _enroll(credential_store, authenticator, sign_count=0)
    client: TestClient = _client(
        pending_store=pending_store,
        credential_store=credential_store,
        principal=Principal(sub=_ACTOR_SUBJECT, iss=_ACTOR_ISSUER),
        audit_store=audit,
    )
    dependencies, _ = _fake_dependencies(
        (_record(_REVIEW_ID),), resolve=resolve or _merge_resolve(audit)
    )
    cast("FastAPI", client.app).dependency_overrides[provide_near_miss_review_dependencies] = (
        lambda: dependencies
    )
    options = client.post(f"/approvals/{row.id}/sign/options", json={"code": code}).json()
    assertion = authenticator.build_assertion(
        rp_id=_RP_ID,
        origin=_ORIGIN,
        challenge=base64url_to_bytes(options["challenge"]),
        sign_count=1,
    )
    response = client.post(
        f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": assertion}
    )
    assert response.status_code == 200
    return response.json(), row.id


def test_signed_merge_records_applied_row_with_approver_as_actor_and_approval_id() -> None:
    audit = InMemoryAuditStore()

    body, approval_id = _sign(audit=audit)

    assert body["status"] == "signed"
    (row,) = audit.rows
    assert (row.actor_subject, row.actor_issuer) == (_ACTOR_SUBJECT, _ACTOR_ISSUER)
    assert (row.action, row.resource_id, row.outcome) == (
        "near_miss.resolve",
        _REVIEW_ID,
        "applied",
    )
    assert row.details["decision"] == "merge"
    assert row.details["approval_id"] == approval_id


def test_signed_merge_row_is_written_before_the_graph_merge() -> None:
    audit = InMemoryAuditStore()

    _sign(audit=audit)

    assert audit.events == ["audit:near_miss.resolve:applied", "graph:merge"]


def test_signed_merge_graph_failure_adds_a_failed_row_with_graph_write_failed() -> None:
    audit = InMemoryAuditStore()

    def _boom(
        graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveOutcome | None:
        _ = (graph, review_id, decision)
        raise CompanyMergePersistenceError("FalkorDB write failed")

    body, approval_id = _sign(audit=audit, resolve=_boom)

    assert "error" in body
    assert [(r.outcome, r.details.get("reason_code")) for r in audit.rows] == [
        ("applied", None),
        ("failed", "graph_write_failed"),
    ]
    assert all(r.details["approval_id"] == approval_id for r in audit.rows)


def test_signed_merge_does_not_run_when_the_opening_row_cannot_be_written_and_stores_the_audit_unavailable_error() -> (  # noqa: E501
    None
):
    audit = InMemoryAuditStore(fail_on_outcome={"applied": AuditPostgresUnavailableError("down")})

    body, _approval_id = _sign(audit=audit)

    assert audit.events == []
    assert body["status"] == "signed"
    error = str(body["error"])
    assert "audit trail is temporarily unavailable" in error
    assert "Request a new approval" in error


def test_merge_via_two_requests_yield_the_same_audit_rows_after_signing() -> None:
    first, second = InMemoryAuditStore(), InMemoryAuditStore()

    _sign(audit=first)
    _sign(audit=second)

    def _shape(store: InMemoryAuditStore) -> list[tuple[object, ...]]:
        return [
            (r.actor_subject, r.action, r.resource_id, r.outcome, sorted(r.details))
            for r in store.rows
        ]

    assert _shape(first) == _shape(second)


def test_merge_audit_row_never_contains_the_approval_code_or_credential() -> None:
    audit = InMemoryAuditStore()

    _sign(audit=audit)

    assert set(audit.rows[0].details) == {
        "review_id",
        "kind",
        "incoming_id",
        "existing_id",
        "decision",
        "approval_id",
    }
