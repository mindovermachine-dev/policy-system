"""HTTP tests for `GET /near-misses` and `POST /near-misses/{review_id}/resolve`.

Mirrors `test_routes_restorations.py`'s style: `TestClient` +
`app.dependency_overrides` supplying a fake `NearMissReviewDependencies`
bundle, so request handling is exercised without a real graph.

The resolve route only keeps a pair separate: any other decision, or a field it does not declare,
is a 422, and no route reports a merge approval.

`_FakePendingApprovalStore` is a local, private copy of
`tests/passkey_signing/_fakes.py`'s own `FakePendingApprovalStore` --
not a cross-package import of it, for the same collection-order reason
`test_near_miss_tools.py` documents in its own copy: `ps-service/tests/api/`
sorts alphabetically *before* `ps-service/tests/passkey_signing/`, so
`from passkey_signing._fakes import ...` at this file's module level cannot
reliably resolve under pytest's `--import-mode=importlib`.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import unquote

import pytest
from company_merge._kinds import UNREGISTERED_KIND
from fastapi.testclient import TestClient

from api._audit_fakes import InMemoryAuditStore
from ps_service import passkey_signing
from ps_service.api.dependencies import (
    get_principal,
    provide_audit_store,
    provide_near_miss_review_dependencies,
    provide_pending_approval_store,
)
from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.api.models import IDENTIFIER_PATTERN
from ps_service.api.near_miss_review_orchestration import NearMissReviewDependencies
from ps_service.audit import AuditPostgresUnavailableError
from ps_service.auth import Principal
from ps_service.company_merge.errors import CompanyMergePersistenceError, UnknownReviewKindError
from ps_service.company_merge.generated_values import SYSTEM_VALUES
from ps_service.company_merge.models import NearMissPair, PendingReviewRecord, ResolveOutcome
from ps_service.company_merge.pending_review import build_pending_review_primitives
from ps_service.company_merge.reviewable_kinds import CAPABILITY
from ps_service.config import ServiceConfig
from ps_service.main import create_app
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.graph_submission import GraphWriteTarget
    from ps_service.company_merge.reviewable_kinds import ReviewableKind
    from ps_service.passkey_signing.store import PendingApprovalStore

_KIND = UNREGISTERED_KIND
_ACTOR_SUBJECT = "user-1"
_ACTOR_ISSUER = "https://issuer.example.com/"


def _principal() -> Principal:
    return Principal(sub=_ACTOR_SUBJECT, iss=_ACTOR_ISSUER)


_CODE_TOKEN_BYTES = 32
_NONCE_BYTES = 32
_EXPIRY_WINDOW = timedelta(minutes=15)


@dataclass
class _FakePendingApprovalStore:
    """In-memory `PendingApprovalStore` (structural `Protocol` match, no real Postgres).

    See this file's own module docstring for why this duplicates
    `tests/passkey_signing/_fakes.py`'s `FakePendingApprovalStore` rather
    than importing it.
    """

    _rows_by_id: dict[str, PendingApprovalRow] = field(default_factory=dict)

    def create_pending_approval(
        self,
        *,
        tool_name: str,
        normalized_args: dict[str, object],
        actor_subject: str,
        actor_issuer: str,
        display_summary: dict[str, object],
    ) -> tuple[PendingApprovalRow, str]:
        code = secrets.token_urlsafe(_CODE_TOKEN_BYTES)
        code_hash = hashlib.sha256(code.encode()).digest()
        nonce = secrets.token_bytes(_NONCE_BYTES)
        created_at = datetime.now(UTC)
        row = PendingApprovalRow(
            id=str(uuid.uuid4()),
            code_hash=code_hash,
            tool_name=tool_name,
            normalized_args=normalized_args,
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            nonce=nonce,
            display_summary=display_summary,
            status="pending",
            outcome=None,
            created_at=created_at,
            expires_at=created_at + _EXPIRY_WINDOW,
        )
        self._rows_by_id[row.id] = row
        return row, code

    def get_by_id(self, pending_approval_id: str) -> PendingApprovalRow | None:
        return self._rows_by_id.get(pending_approval_id)

    def get_by_code_hash(self, code_hash: bytes) -> PendingApprovalRow | None:
        for row in self._rows_by_id.values():
            if row.code_hash == code_hash:
                return row
        return None

    def mark_signed(self, pending_approval_id: str) -> bool:
        """Mirrors `tests/passkey_signing/_fakes.py`'s own `FakePendingApprovalStore.mark_signed`
        -- kept structurally complete so this duplicate still
        satisfies the `PendingApprovalStore` `Protocol`, even though no test in
        this file drives the signing ceremony itself (that's `passkey_signing/
        test_signing_ceremony.py`'s job).
        """
        row = self._rows_by_id.get(pending_approval_id)
        if row is None or row.status != "pending":
            return False
        self._rows_by_id[pending_approval_id] = replace(row, status="signed")
        return True

    def set_outcome(self, pending_approval_id: str, outcome: dict[str, object]) -> None:
        row = self._rows_by_id.get(pending_approval_id)
        if row is not None:
            self._rows_by_id[pending_approval_id] = replace(row, outcome=outcome)


@dataclass
class _FakeGraphHandle:
    """Never actually queried by these fakes -- `list_pending_reviews` is scripted directly."""


def _record(
    review_id: str = "review_aaa",
    *,
    kind: str = "Capability",
    similarity: float = 0.62,
) -> PendingReviewRecord:
    return PendingReviewRecord(
        id=review_id,
        kind=kind,
        incoming_id="capability_incoming_a",
        incoming_text="Report the incident to the authority.",
        nearest_existing_id="capability_existing_a",
        nearest_existing_text="Conduct a risk assessment.",
        similarity=similarity,
        created_at="2026-01-01T00:00:00+00:00",
    )


def _unexpected_resolve_review(
    graph: GraphHandle,
    review_id: str,
    decision: Literal["keep-separate", "merge"],
    target: GraphWriteTarget,
) -> ResolveOutcome | None:
    """Default `resolve_review` stub for a list-only test -- fails loudly if ever called."""
    _ = graph, review_id, decision
    raise AssertionError("resolve_review should not be called in this test")


def _fake_dependencies(
    records: tuple[PendingReviewRecord, ...],
    *,
    resolve: Callable[
        [GraphHandle, str, Literal["keep-separate", "merge"], GraphWriteTarget],
        ResolveOutcome | None,
    ]
    | None = None,
    list_error: Exception | None = None,
    get_error: Exception | None = None,
) -> tuple[NearMissReviewDependencies, list[ServiceConfig]]:
    open_calls: list[ServiceConfig] = []

    def _open_single_tenant_graph(config: ServiceConfig) -> GraphHandle:
        open_calls.append(config)
        return cast("GraphHandle", _FakeGraphHandle())

    def _list_pending_reviews(graph: GraphHandle) -> tuple[PendingReviewRecord, ...]:
        _ = graph
        if list_error is not None:
            raise list_error
        return records

    def _get_pending_review(graph: GraphHandle, review_id: str) -> PendingReviewRecord | None:
        _ = graph
        if get_error is not None:
            raise get_error
        return next((r for r in records if r.id == review_id), None)

    dependencies = NearMissReviewDependencies(
        open_single_tenant_graph=_open_single_tenant_graph,
        list_pending_reviews=_list_pending_reviews,
        resolve_review=resolve or _unexpected_resolve_review,
        get_pending_review=_get_pending_review,
    )
    return dependencies, open_calls


def _app_config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )


def _client_with_records(records: tuple[PendingReviewRecord, ...]) -> TestClient:
    dependencies, _ = _fake_dependencies(records)
    app = create_app(_app_config())
    app.dependency_overrides[provide_near_miss_review_dependencies] = lambda: dependencies
    return TestClient(app)


def test_get_near_misses_empty_graph_returns_empty_reviews_list() -> None:
    client = _client_with_records(())

    response = client.get("/near-misses")

    assert response.status_code == 200
    assert response.json() == {"reviews": []}


def test_get_near_misses_returns_id_incoming_text_existing_text_similarity() -> None:
    """Every unresolved PendingReview is shown with id, texts, similarity."""
    record = _record()
    client = _client_with_records((record,))

    response = client.get("/near-misses")

    assert response.status_code == 200
    body = response.json()
    assert len(body["reviews"]) == 1
    entry = body["reviews"][0]
    assert entry["id"] == record.id
    assert entry["kind"] == record.kind
    assert entry["incoming_text"] == record.incoming_text
    assert entry["nearest_existing_text"] == record.nearest_existing_text
    assert entry["similarity"] == record.similarity
    # The response requires only id/incoming text/existing text/similarity -- the
    # underlying node ids are deliberately not part of the wire response.
    assert "incoming_id" not in entry
    assert "nearest_existing_id" not in entry


def test_get_near_misses_returns_every_unresolved_review_in_order(
    test_only_reviewable_kind: ReviewableKind,
) -> None:
    first = _record("review_aaa", kind="Capability", similarity=0.6)
    second = _record("review_bbb", kind=test_only_reviewable_kind.label, similarity=0.75)
    client = _client_with_records((first, second))

    response = client.get("/near-misses")

    assert response.status_code == 200
    reviews = response.json()["reviews"]
    assert [(entry["id"], entry["kind"]) for entry in reviews] == [
        ("review_aaa", "Capability"),
        ("review_bbb", "ProbeItem"),
    ]


def test_listing_with_a_review_of_an_unregistered_kind_answers_409_naming_it() -> None:
    dependencies, _ = _fake_dependencies(
        (), list_error=UnknownReviewKindError(f"no reviewable kind is registered for {_KIND!r}")
    )
    app = create_app(_app_config())
    app.dependency_overrides[provide_near_miss_review_dependencies] = lambda: dependencies

    response = TestClient(app).get("/near-misses")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "review_kind_unsupported"
    assert _KIND in error["message"]


def test_get_near_misses_opens_the_single_tenant_graph_via_injected_config() -> None:
    dependencies, open_calls = _fake_dependencies(())
    app = create_app(_app_config())
    app.dependency_overrides[provide_near_miss_review_dependencies] = lambda: dependencies
    client = TestClient(app)

    client.get("/near-misses")

    assert len(open_calls) == 1


def test_get_near_misses_is_unauthenticated_never_401_or_403() -> None:
    client = _client_with_records(())

    response = client.get("/near-misses")

    assert response.status_code not in (401, 403)


def _client_for_resolve(
    resolve: Callable[
        [GraphHandle, str, Literal["keep-separate", "merge"], GraphWriteTarget],
        ResolveOutcome | None,
    ],
    *,
    records: tuple[PendingReviewRecord, ...] = (),
    principal: Principal | None = None,
    store: PendingApprovalStore | None = None,
    audit_store: InMemoryAuditStore | None = None,
    get_error: Exception | None = None,
) -> TestClient:
    dependencies, _ = _fake_dependencies(records, resolve=resolve, get_error=get_error)
    app = create_app(_app_config())
    audit = audit_store or InMemoryAuditStore()
    app.dependency_overrides[provide_audit_store] = lambda: audit
    app.dependency_overrides[provide_near_miss_review_dependencies] = lambda: dependencies
    app.dependency_overrides[provide_pending_approval_store] = lambda: (
        store or _FakePendingApprovalStore()
    )
    if principal is not None:
        app.dependency_overrides[get_principal] = lambda: principal
    return TestClient(app)


def test_post_resolve_keep_separate_returns_the_resolved_review() -> None:
    """`decision="keep-separate"` succeeds and echoes the resolved review id and the decision,
    in one synchronous round trip with no principal required.
    """
    recorded_calls: list[tuple[str, str]] = []

    def _resolve(
        graph: GraphHandle,
        review_id: str,
        decision: Literal["keep-separate", "merge"],
        target: GraphWriteTarget,
    ) -> ResolveOutcome | None:
        _ = graph
        recorded_calls.append((review_id, decision))
        return ResolveOutcome(review_id=review_id, decision=decision)

    client = _client_for_resolve(_resolve, records=(_record("review_aaa"),))

    response = client.post("/near-misses/review_aaa/resolve", json={"decision": "keep-separate"})

    assert response.status_code == 200
    body = response.json()
    assert body == {"review_id": "review_aaa", "decision": "keep-separate"}
    assert recorded_calls == [("review_aaa", "keep-separate")]


def test_resolving_a_review_of_an_unregistered_kind_answers_409_naming_it_and_audits_nothing() -> (
    None
):
    audit_store = InMemoryAuditStore()
    client = _client_for_resolve(
        _unexpected_resolve_review,
        audit_store=audit_store,
        get_error=UnknownReviewKindError(f"no reviewable kind is registered for {_KIND!r}"),
    )

    response = client.post("/near-misses/review_aaa/resolve", json={"decision": "keep-separate"})

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "review_kind_unsupported"
    assert _KIND in error["message"]
    assert audit_store.rows == []


def test_keep_separate_over_rest_writes_no_access_gate_row() -> None:
    """Only the merge decision is role-gated, so keep-separate never writes a gate row."""

    def _resolve(
        graph: GraphHandle,
        review_id: str,
        decision: Literal["keep-separate", "merge"],
        target: GraphWriteTarget,
    ) -> ResolveOutcome | None:
        _ = (graph, target)
        return ResolveOutcome(review_id=review_id, decision=decision)

    audit = InMemoryAuditStore()
    client = _client_for_resolve(_resolve, records=(_record("review_aaa"),), audit_store=audit)

    response = client.post("/near-misses/review_aaa/resolve", json={"decision": "keep-separate"})

    assert response.status_code == 200
    assert [row for row in audit.rows if row.action == "access.gate"] == []


def test_post_resolve_keep_separate_without_a_gateway_is_503_and_writes_no_audit_row() -> None:
    """Keep-separate writes through the gateway; none installed fails closed."""
    install_graph_write_gateway(None)
    audit = InMemoryAuditStore()

    def _resolve(
        graph: GraphHandle,
        review_id: str,
        decision: Literal["keep-separate", "merge"],
        target: GraphWriteTarget,
    ) -> ResolveOutcome | None:
        raise AssertionError("the resolution must not run without a gateway")

    client = _client_for_resolve(_resolve, records=(_record("review_aaa"),), audit_store=audit)

    response = client.post("/near-misses/review_aaa/resolve", json={"decision": "keep-separate"})

    assert response.status_code == 503
    assert audit.rows == []


def test_post_resolve_persistence_failure_is_a_generic_500_with_a_failed_audit_row() -> None:
    """A refused or unapplied graph write is classified as a generic 500.

    The route answers the generic 500 (no gateway text, host or port in the body) and the
    resolution's follow-up audit row records `graph_write_failed`.
    """
    audit = InMemoryAuditStore()

    def _resolve(
        graph: GraphHandle,
        review_id: str,
        decision: Literal["keep-separate", "merge"],
        target: GraphWriteTarget,
    ) -> ResolveOutcome | None:
        raise CompanyMergePersistenceError("FalkorDB write failed: could not reach 10.0.0.5:6379")

    client = TestClient(
        _client_for_resolve(_resolve, records=(_record("review_aaa"),), audit_store=audit).app,
        raise_server_exceptions=False,  # the catch-all handler answers; do not re-raise it here
    )

    response = client.post("/near-misses/review_aaa/resolve", json={"decision": "keep-separate"})

    assert response.status_code == 500
    for leak in ("10.0.0.5", "6379", "FalkorDB", "committed_apply_pending"):
        assert leak not in response.text
    assert [(r.outcome, r.details.get("reason_code")) for r in audit.rows] == [
        ("applied", None),
        ("failed", "graph_write_failed"),
    ]


def test_post_resolve_unknown_id_returns_404_pending_review_not_found() -> None:
    """A bad id surfaces as a clear 404, not a 500/200."""

    def _resolve(
        graph: GraphHandle,
        review_id: str,
        decision: Literal["keep-separate", "merge"],
        target: GraphWriteTarget,
    ) -> ResolveOutcome | None:
        _ = graph, review_id, decision
        return None

    client = _client_for_resolve(_resolve)

    response = client.post(
        "/near-misses/review_missing/resolve", json={"decision": "keep-separate"}
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "pending_review_not_found"
    assert "review_missing" in body["error"]["message"]


def test_post_resolve_rejects_unknown_decision_with_422() -> None:
    """Pydantic's `Literal["keep-separate"]` rejects any other value.

    `resolve_review` is never called: the default `_unexpected_resolve_review`
    stub would raise `AssertionError` if it were, and this test passes --
    proving validation rejects an unknown decision before the route body
    ever runs.
    """
    dependencies, _ = _fake_dependencies(())
    app = create_app(_app_config())
    app.dependency_overrides[provide_near_miss_review_dependencies] = lambda: dependencies
    client = TestClient(app)

    response = client.post("/near-misses/review_aaa/resolve", json={"decision": "not-a-decision"})

    assert response.status_code == 422


def test_a_merge_decision_is_a_422() -> None:
    dependencies, _ = _fake_dependencies(())
    app = create_app(_app_config())
    app.dependency_overrides[provide_near_miss_review_dependencies] = lambda: dependencies
    app.dependency_overrides[get_principal] = _principal

    response = TestClient(app).post("/near-misses/review_aaa/resolve", json={"decision": "merge"})

    assert response.status_code == 422


def test_the_acknowledgment_field_is_a_422() -> None:
    dependencies, _ = _fake_dependencies(())
    app = create_app(_app_config())
    app.dependency_overrides[provide_near_miss_review_dependencies] = lambda: dependencies
    app.dependency_overrides[get_principal] = _principal

    response = TestClient(app).post(
        "/near-misses/review_aaa/resolve",
        json={"decision": "keep-separate", "acknowledge_governance_change": True},
    )

    assert response.status_code == 422


def test_the_approval_status_path_is_no_longer_routed() -> None:
    app = create_app(_app_config())
    app.dependency_overrides[get_principal] = _principal

    response = TestClient(app).get(f"/near-misses/approvals/{uuid.uuid4()}")

    assert response.status_code in (404, 405)
    assert "error" not in response.json()
    assert not [p for p in app.openapi()["paths"] if p.startswith("/near-misses/approvals")]


def test_keep_separate_is_open_to_any_authenticated_caller() -> None:
    """No access-role store is configured in this test, so any role lookup on the keep-separate
    path would fail the request instead of resolving it.
    """
    client = _client_for_resolve(
        _outcome_resolve, records=(_record("review_aaa"),), principal=_principal()
    )

    response = client.post("/near-misses/review_aaa/resolve", json={"decision": "keep-separate"})

    assert response.status_code == 200
    assert response.json()["decision"] == "keep-separate"


# --- keep-separate audits `near_miss.resolve` ---


def _outcome_resolve(
    graph: GraphHandle,
    review_id: str,
    decision: Literal["keep-separate", "merge"],
    target: GraphWriteTarget,
) -> ResolveOutcome | None:
    _ = graph
    return ResolveOutcome(review_id=review_id, decision=decision)


def test_post_resolve_keep_separate_audits_the_principal() -> None:
    audit = InMemoryAuditStore()
    client = _client_for_resolve(
        _outcome_resolve,
        records=(_record("review_aaa"),),
        principal=_principal(),
        audit_store=audit,
    )

    response = client.post("/near-misses/review_aaa/resolve", json={"decision": "keep-separate"})

    assert response.status_code == 200
    (row,) = audit.rows
    assert (row.actor_subject, row.actor_issuer) == (_ACTOR_SUBJECT, _ACTOR_ISSUER)
    assert (row.action, row.resource_id, row.outcome) == (
        "near_miss.resolve",
        "review_aaa",
        "applied",
    )
    assert row.details["decision"] == "keep_separate"


def test_post_resolve_keep_separate_under_bypass_audits_the_sentinel() -> None:
    audit = InMemoryAuditStore()
    client = _client_for_resolve(
        _outcome_resolve, records=(_record("review_aaa"),), audit_store=audit
    )

    client.post("/near-misses/review_aaa/resolve", json={"decision": "keep-separate"})

    (row,) = audit.rows
    assert (row.actor_subject, row.actor_issuer) == ("system:local-test-bypass",) * 2


def test_post_resolve_returns_503_when_audit_is_unavailable_and_graph_is_untouched() -> None:
    audit = InMemoryAuditStore(fail_on_outcome={"applied": AuditPostgresUnavailableError("down")})
    resolve_calls: list[str] = []

    def _resolve(
        graph: GraphHandle,
        review_id: str,
        decision: Literal["keep-separate", "merge"],
        target: GraphWriteTarget,
    ) -> ResolveOutcome | None:
        resolve_calls.append(review_id)
        return _outcome_resolve(graph, review_id, decision, target)

    client = _client_for_resolve(
        _resolve, records=(_record("review_aaa"),), principal=_principal(), audit_store=audit
    )

    response = client.post("/near-misses/review_aaa/resolve", json={"decision": "keep-separate"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "audit_trail_unavailable"
    assert resolve_calls == []


_MALFORMED_IDS = [
    pytest.param("r" * 65, id="65-characters"),
    pytest.param("review.aaa", id="dot"),
    pytest.param("review%20aaa", id="space"),
    pytest.param("review;aaa", id="semicolon"),
    pytest.param("review_aaa%0A", id="trailing-newline"),
    pytest.param("review%C3%A9", id="non-ascii-letter"),
]


@dataclass
class _RecordingPendingApprovalStore(_FakePendingApprovalStore):
    """The fake store, noting every id it is asked to look up."""

    looked_up: list[str] = field(default_factory=list)

    def get_by_id(self, pending_approval_id: str) -> PendingApprovalRow | None:
        self.looked_up.append(pending_approval_id)
        return super().get_by_id(pending_approval_id)


def _generic_validation_body(client: TestClient) -> dict[str, object]:
    """The body this app returns for any request that fails validation."""
    response = client.post("/near-misses/review_aaa/resolve", json={})
    assert response.status_code == 422
    return response.json()


@pytest.mark.parametrize("review_id", _MALFORMED_IDS)
def test_resolve_rejects_a_malformed_review_id_with_the_generic_422_and_calls_nothing(
    review_id: str,
) -> None:
    dependencies, open_calls = _fake_dependencies((_record("review_aaa"),))
    store = _RecordingPendingApprovalStore()
    audit = InMemoryAuditStore()
    app = create_app(_app_config())
    app.dependency_overrides[provide_near_miss_review_dependencies] = lambda: dependencies
    app.dependency_overrides[provide_pending_approval_store] = lambda: store
    app.dependency_overrides[provide_audit_store] = lambda: audit
    app.dependency_overrides[get_principal] = _principal
    client = TestClient(app)
    expected_body = _generic_validation_body(client)
    open_calls.clear()

    response = client.post(f"/near-misses/{review_id}/resolve", json={"decision": "keep-separate"})

    assert response.status_code == 422
    assert response.json() == expected_body
    assert unquote(review_id) not in response.text
    assert open_calls == []
    assert store.looked_up == []
    assert audit.rows == []


def test_review_id_from_the_real_generator_satisfies_the_path_pattern() -> None:
    pairs = tuple(
        NearMissPair(
            incoming_id=f"capability_incoming_{index}",
            incoming_text="Report the incident to the authority.",
            nearest_existing_id=f"capability_existing_{index}",
            nearest_existing_text="Conduct a risk assessment.",
            similarity=0.62,
        )
        for index in range(3)
    )

    primitives = build_pending_review_primitives(pairs, kind=CAPABILITY, values=SYSTEM_VALUES)

    assert len(primitives) == 3
    for primitive in primitives:
        assert re.fullmatch(IDENTIFIER_PATTERN, primitive.id)


def test_resolving_a_review_with_a_generated_id_returns_the_resolved_review() -> None:
    pair = NearMissPair(
        incoming_id="capability_incoming_a",
        incoming_text="Report the incident to the authority.",
        nearest_existing_id="capability_existing_a",
        nearest_existing_text="Conduct a risk assessment.",
        similarity=0.62,
    )
    (primitive,) = build_pending_review_primitives((pair,), kind=CAPABILITY, values=SYSTEM_VALUES)

    def _resolve(
        graph: GraphHandle,
        review_id: str,
        decision: Literal["keep-separate", "merge"],
        target: GraphWriteTarget,
    ) -> ResolveOutcome | None:
        _ = (graph, target)
        return ResolveOutcome(review_id=review_id, decision=decision)

    client = _client_for_resolve(_resolve, records=(_record(primitive.id),))

    response = client.post(
        f"/near-misses/{primitive.id}/resolve", json={"decision": "keep-separate"}
    )

    assert response.status_code == 200
    assert response.json()["review_id"] == primitive.id
    assert response.json()["decision"] == "keep-separate"


def test_the_pending_approval_column_generates_ids_that_satisfy_the_path_pattern() -> None:
    migration = Path(passkey_signing.__file__).parent / "migrations" / "0001_pending_approvals.sql"

    assert "id uuid PRIMARY KEY DEFAULT gen_random_uuid()" in migration.read_text()
    assert re.fullmatch(IDENTIFIER_PATTERN, str(uuid.uuid4()))
