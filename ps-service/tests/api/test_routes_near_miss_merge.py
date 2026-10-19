"""The near-miss merge and status routes over the graph-cleanup approval.

`POST /near-misses/{id}/resolve` (merge) and `GET /near-misses/approvals/{id}`.

REST is the second entry point to the graph-cleanup `merge-capabilities` approval, next to the MCP
tool. Over a `CleanupRig` (real reader, planner, executor and gateway rig; only the stores and the
signing ceremony are doubles) these tests drive the routes as a caller: the `ComplianceOfficer`
check that applies to the merge branch only, the preview plus approval link, the Policy cases, the
`acknowledge_governance_change` body field, and the status route reporting `winner_id` and
`loser_id` through the shared mapper the MCP tool also uses.
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING

import pytest
from authz._fakes import FakeAccessRoleStore
from fastapi.testclient import TestClient
from graph_cleanup._fakes import ABSORBED, SURVIVOR, FakeApprovalStore, approval_row_count
from graph_cleanup._gateway_fakes import CleanupReadGraph, CleanupRig
from graph_cleanup._mcp_calls import factory
from graph_cleanup._merge_runs import CONFIG, OFFICER, OWNER, roles
from graph_gateway._fakes import close_all_rigs

from api._audit_fakes import InMemoryAuditStore
from ps_service.api import dependencies as api_dependencies
from ps_service.api.dependencies import (
    get_principal,
    provide_audit_store,
    provide_graph_cleanup_dependencies,
    provide_near_miss_review_dependencies,
    provide_pending_approval_store,
)
from ps_service.api.near_miss_review_orchestration import (
    build_default_near_miss_review_dependencies,
)
from ps_service.auth import Principal
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.executors import execute_capability_merge
from ps_service.logging import configure
from ps_service.main import create_app
from ps_service.passkey_signing.errors import PendingApprovalPersistenceError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import NoReturn

_REVIEW = "review_aaa"
_OLD = "2020-01-01T00:00:00+00:00"
_NEW = "2024-01-01T00:00:00+00:00"
_PLAIN = ("plain-user", OWNER[1])
_REST_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=True,
    authentik_api_token="test-authentik-token",
    authentik_base_url="https://authentik.example.com",
)


class _Env:
    """A rig world with a review of (ABSORBED incoming, SURVIVOR existing), routes wired to it."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        rig: CleanupRig,
        *,
        principal: tuple[str, str] | None = OFFICER,
        access_roles: FakeAccessRoleStore | None = None,
    ) -> None:
        self.rig = rig
        self.store = FakeApprovalStore()
        self.audit = InMemoryAuditStore()
        rig.seed_capability_merge_world()
        rig.seed_near_miss_review(
            _REVIEW,
            incoming_id=ABSORBED,
            existing_id=SURVIVOR,
            incoming_created_at=_NEW,
            existing_created_at=_OLD,
        )
        monkeypatch.setattr(
            api_dependencies, "PsycopgAccessRoleStore", factory(access_roles or roles())
        )

        def _open(_config: object) -> CleanupReadGraph:
            return rig.read

        app = create_app(_REST_CONFIG)
        app.dependency_overrides[provide_near_miss_review_dependencies] = lambda: (
            build_default_near_miss_review_dependencies(open_single_tenant_graph=_open)
        )
        app.dependency_overrides[provide_graph_cleanup_dependencies] = lambda: rig.dependencies(
            roles()
        )
        app.dependency_overrides[provide_pending_approval_store] = lambda: self.store
        app.dependency_overrides[provide_audit_store] = lambda: self.audit
        if principal is not None:
            caller = Principal(sub=principal[0], iss=principal[1])
            app.dependency_overrides[get_principal] = lambda: caller
        self.app = app
        self.client = TestClient(app)

    def status(
        self, approval_id: str, *, as_caller: tuple[str, str] | None
    ) -> tuple[int, dict[str, dict[str, str]]]:
        """GET the approval's status as another caller (None: no verified identity)."""
        if as_caller is None:
            self.app.dependency_overrides[get_principal] = lambda: None
        else:
            who = Principal(sub=as_caller[0], iss=as_caller[1])
            self.app.dependency_overrides[get_principal] = lambda: who
        response = self.client.get(f"/near-misses/approvals/{approval_id}")
        return response.status_code, response.json()

    def snapshot(self) -> object:
        return copy.deepcopy(dict(self.rig.graph.nodes)), set(self.rig.graph.edges)

    def merge(
        self, *, acknowledge: bool | None = None, review_id: str = _REVIEW
    ) -> tuple[int, dict[str, object]]:
        body: dict[str, object] = {"decision": "merge"}
        if acknowledge is not None:
            body["acknowledge_governance_change"] = acknowledge
        response = self.client.post(f"/near-misses/{review_id}/resolve", json=body)
        return response.status_code, response.json()

    def sign_and_execute(self, approval_id: str) -> dict[str, object]:
        """Sign the way the router does: mark signed, run the executor, store the outcome."""
        assert self.store.mark_signed(approval_id)
        signed = self.store.get_by_id(approval_id)
        assert signed is not None
        outcome = execute_capability_merge(signed, CONFIG, self.rig.dependencies(roles()))
        self.store.set_outcome(approval_id, outcome)
        return outcome


@pytest.fixture
def cleanup_rig() -> Iterator[CleanupRig]:
    configure()
    rig = CleanupRig()
    yield rig
    close_all_rigs()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, cleanup_rig: CleanupRig) -> _Env:
    return _Env(monkeypatch, cleanup_rig)


def _governed_by(env: _Env, capability_id: str) -> list[str]:
    return [
        key[4] for key in env.rig.graph.edges if key[0] == "GOVERNED_BY" and key[2] == capability_id
    ]


# --- who may merge --------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["none", "system_admin", "system_owner"])
def test_a_caller_without_an_explicit_compliance_officer_grant_gets_403_and_no_approval(
    monkeypatch: pytest.MonkeyPatch, cleanup_rig: CleanupRig, role: str
) -> None:
    access_roles = FakeAccessRoleStore(expected_owner=OWNER)
    access_roles.bootstrap_first_owner(OWNER)
    caller = _PLAIN
    if role == "system_owner":
        caller = OWNER
    elif role == "system_admin":
        access_roles.grant(actor=OWNER, target=caller, access_role=AccessRole.SYSTEM_ADMIN)
    environment = _Env(monkeypatch, cleanup_rig, principal=caller, access_roles=access_roles)
    before = environment.snapshot()

    status, body = environment.merge()

    assert status == 403
    assert body["error"]["code"] == "access_denied"  # pyright: ignore[reportIndexIssue]
    assert approval_row_count(environment.store) == 0
    assert environment.snapshot() == before


def test_an_unauthenticated_merge_is_401_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, cleanup_rig: CleanupRig
) -> None:
    environment = _Env(monkeypatch, cleanup_rig, principal=None)
    before = environment.snapshot()

    status, body = environment.merge()

    assert status == 401
    assert body["error"]["code"] == "merge_approval_requires_authenticated_caller"  # pyright: ignore[reportIndexIssue]
    assert approval_row_count(environment.store) == 0
    assert environment.snapshot() == before


def test_merge_denial_over_rest_writes_one_access_gate_row(
    monkeypatch: pytest.MonkeyPatch, cleanup_rig: CleanupRig
) -> None:
    environment = _Env(monkeypatch, cleanup_rig, principal=_PLAIN)

    status, _body = environment.merge()

    assert status == 403
    (row,) = environment.audit.rows
    assert (row.actor_subject, row.actor_issuer) == _PLAIN
    assert (row.action, row.outcome, row.resource_type) == ("access.gate", "rejected", "operation")
    assert row.resource_id == "POST /near-misses/{review_id}/resolve"
    assert row.details == {"required_role": "ComplianceOfficer"}


def test_merge_without_a_verified_principal_writes_no_access_gate_row(
    monkeypatch: pytest.MonkeyPatch, cleanup_rig: CleanupRig
) -> None:
    environment = _Env(monkeypatch, cleanup_rig, principal=None)

    status, _body = environment.merge()

    assert status == 401
    assert environment.audit.rows == []


def test_permitted_merge_over_rest_writes_no_access_gate_row(env: _Env) -> None:
    status, _body = env.merge()

    assert status == 200
    assert env.audit.rows == []


# --- case 1 ---------------------------------------------------------------------------------


def test_merge_returns_the_preview_and_an_approval_link_and_leaves_the_graph_unchanged(
    env: _Env,
) -> None:
    before = env.snapshot()

    status, body = env.merge()

    assert status == 200
    assert body["review_id"] == _REVIEW
    assert body["decision"] == "merge"
    assert body["winner_id"] is None
    assert body["loser_id"] is None
    assert body["acknowledgment_required"] is None
    assert body["pending_approval_id"]
    assert body["expires_at"]
    preview = body["preview"]
    assert preview["survivor_id"] == SURVIVOR  # pyright: ignore[reportIndexIssue]
    assert preview["absorbed_id"] == ABSORBED  # pyright: ignore[reportIndexIssue]
    assert preview["policy_case"] == 1  # pyright: ignore[reportIndexIssue]
    assert preview["edges_to_move"]["covers"] == ["pa_1"]  # pyright: ignore[reportIndexIssue]
    row = env.store.get_by_id(str(body["pending_approval_id"]))
    assert row is not None
    assert row.tool_name == "merge-capabilities"
    assert row.normalized_args["review_id"] == _REVIEW
    assert (row.actor_subject, row.actor_issuer) == OFFICER
    assert env.snapshot() == before
    assert env.audit.rows == []


def test_merge_of_an_unknown_review_is_404_and_creates_no_approval(env: _Env) -> None:
    status, body = env.merge(review_id="review_missing")

    assert status == 404
    assert body["error"]["code"] == "pending_review_not_found"  # pyright: ignore[reportIndexIssue]
    assert "review_missing" in body["error"]["message"]  # pyright: ignore[reportIndexIssue]
    assert approval_row_count(env.store) == 0


def test_merge_of_a_policy_review_is_refused_and_creates_no_approval(env: _Env) -> None:
    env.rig.seed_near_miss_review(
        "review_pol", incoming_id="pol_a", existing_id="pol_b", kind="Policy"
    )

    status, body = env.merge(review_id="review_pol")

    assert status == 409
    assert body["error"]["code"] == "near_miss_merge_refused"  # pyright: ignore[reportIndexIssue]
    assert approval_row_count(env.store) == 0


# --- the approval link ----------------------------------------------------------------------


def test_the_approval_url_carries_the_code_only_after_the_fragment(env: _Env) -> None:
    _, body = env.merge()

    prefix, _, code = str(body["approval_url"]).partition("#")
    assert code
    assert prefix.endswith(f"/approvals/{body['pending_approval_id']}")
    assert code != body["pending_approval_id"]


@pytest.mark.parametrize(
    ("host", "expected_prefix"),
    [
        ("127.0.0.1:8000", "http://localhost:8000/approvals/"),
        ("[::1]:8000", "http://localhost:8000/approvals/"),
        ("10.1.2.3:30080", "http://10.1.2.3:30080/approvals/"),
    ],
)
def test_a_loopback_host_is_rewritten_to_localhost_and_other_hosts_are_untouched(
    env: _Env, host: str, expected_prefix: str
) -> None:
    response = env.client.post(
        f"/near-misses/{_REVIEW}/resolve", json={"decision": "merge"}, headers={"Host": host}
    )

    assert response.status_code == 200
    assert response.json()["approval_url"].startswith(expected_prefix)


# --- Policy cases ---------------------------------------------------------------------------


def test_two_differently_governed_capabilities_give_an_error_naming_the_release_step(
    env: _Env,
) -> None:
    env.rig.seed_policy("pol_a", title="A", status="draft")
    env.rig.seed_policy("pol_b", title="B", status="draft")
    env.rig.seed_governs(SURVIVOR, "pol_a")
    env.rig.seed_governs(ABSORBED, "pol_b")
    before = env.snapshot()

    status, body = env.merge()

    assert status == 409
    assert body["error"]["code"] == "near_miss_merge_refused"  # pyright: ignore[reportIndexIssue]
    assert "release-capability-governance" in body["error"]["message"]  # pyright: ignore[reportIndexIssue]
    assert approval_row_count(env.store) == 0
    assert env.snapshot() == before
    assert ("PendingReview", _REVIEW) in env.rig.graph.nodes


def test_exactly_one_governed_capability_returns_the_preview_asking_for_the_acknowledgment(
    env: _Env,
) -> None:
    env.rig.seed_policy("pol_1", title="Incident Policy", status="approved")
    env.rig.seed_governs(ABSORBED, "pol_1")
    before = env.snapshot()

    status, body = env.merge()

    assert status == 200
    assert body["acknowledgment_required"] is True
    assert body["preview"]["policy_case"] == 2  # pyright: ignore[reportIndexIssue]
    assert "acknowledge_governance_change=true" in str(body["message"])
    assert body["pending_approval_id"] is None
    assert body["approval_url"] is None
    assert body["expires_at"] is None
    assert approval_row_count(env.store) == 0
    assert env.snapshot() == before


def test_with_the_acknowledgment_field_the_approval_is_created_and_the_winner_ends_up_governed(
    env: _Env,
) -> None:
    env.rig.seed_policy("pol_1", title="Incident Policy", status="approved")
    env.rig.seed_governs(ABSORBED, "pol_1")

    status, body = env.merge(acknowledge=True)
    env.sign_and_execute(str(body["pending_approval_id"]))

    assert status == 200
    assert body["preview"]["policy_case"] == 2  # pyright: ignore[reportIndexIssue]
    assert body["acknowledgment_required"] is None
    row = env.store.get_by_id(str(body["pending_approval_id"]))
    assert row is not None
    assert row.normalized_args["acknowledge_governance_change"] is True
    assert _governed_by(env, SURVIVOR) == ["pol_1"]


def test_an_unknown_body_field_is_rejected_with_422(env: _Env) -> None:
    response = env.client.post(
        f"/near-misses/{_REVIEW}/resolve", json={"decision": "merge", "acknowledge": True}
    )

    assert response.status_code == 422


# --- the status route -----------------------------------------------------------------------


def test_the_status_route_reports_pending_then_the_winner_and_loser_once_signed(env: _Env) -> None:
    _, created = env.merge()
    approval_id = str(created["pending_approval_id"])

    pending = env.client.get(f"/near-misses/approvals/{approval_id}")
    env.sign_and_execute(approval_id)
    signed = env.client.get(f"/near-misses/approvals/{approval_id}")

    assert pending.status_code == 200
    assert pending.json() == {
        "pending_approval_id": approval_id,
        "status": "pending",
        "review_id": _REVIEW,
        "decision": "merge",
        "winner_id": None,
        "loser_id": None,
        "error": None,
    }
    assert signed.status_code == 200
    body = signed.json()
    assert body["status"] == "signed"
    assert (body["winner_id"], body["loser_id"]) == (SURVIVOR, ABSORBED)
    assert body["error"] is None
    assert env.rig.graph.nodes[("Capability", ABSORBED)]["status"] == "merged"
    assert ("PendingReview", _REVIEW) not in env.rig.graph.nodes


def test_the_status_route_reports_the_executors_error_for_a_signed_approval_that_failed(
    env: _Env,
) -> None:
    _, created = env.merge()
    approval_id = str(created["pending_approval_id"])
    env.store.mark_signed(approval_id)
    env.store.set_outcome(approval_id, {"error": "the merge could not be applied"})

    body = env.client.get(f"/near-misses/approvals/{approval_id}").json()

    assert body["status"] == "signed"
    assert (body["winner_id"], body["loser_id"]) == (None, None)
    assert body["error"] == "the merge could not be applied"


def test_the_status_route_hides_unknown_ids_other_callers_and_unauthenticated_callers(
    env: _Env,
) -> None:
    _, created = env.merge()
    approval_id = str(created["pending_approval_id"])

    responses = [
        env.status("nope", as_caller=OFFICER),
        env.status(approval_id, as_caller=_PLAIN),
        env.status(approval_id, as_caller=None),
    ]

    assert [code for code, _body in responses] == [404, 404, 404]
    assert {body["error"]["code"] for _code, body in responses} == {"pending_approval_not_found"}


def test_the_status_route_refuses_a_merge_capabilities_row_without_a_review_id(env: _Env) -> None:
    row, _code = env.store.create_pending_approval(
        tool_name="merge-capabilities",
        normalized_args={
            "survivor_id": SURVIVOR,
            "absorbed_id": ABSORBED,
            "acknowledge_governance_change": False,
            "state_digest": "d",
        },
        actor_subject=OFFICER[0],
        actor_issuer=OFFICER[1],
        display_summary={},
    )

    response = env.client.get(f"/near-misses/approvals/{row.id}")

    assert response.status_code == 404
    assert json.dumps(response.json()).count("pending_approval_not_found") == 1


# --- failures while creating the approval (create leg) ----------------------------

_LEAKS = ("10.0.0.1", "6379", "hunter2", "/secret/path")


class _FailingStore:
    def __init__(self, failure: Exception) -> None:
        self._failure = failure

    def create_pending_approval(self, **kwargs: object) -> object:
        _ = kwargs
        raise self._failure


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionError("connection refused to 10.0.0.1:6379 password=hunter2"),
        PendingApprovalPersistenceError("insert failed on 10.0.0.1 password=hunter2"),
        RuntimeError("boom at /secret/path password=hunter2"),
    ],
    ids=["graph-unreachable", "approval-store-unreachable", "unexpected"],
)
def test_a_failure_creating_the_approval_gives_the_generic_500_leaks_nothing_and_writes_nothing(
    env: _Env, failure: Exception
) -> None:
    def _unreachable(_config: object) -> NoReturn:
        raise failure

    if isinstance(failure, ConnectionError):
        env.app.dependency_overrides[provide_near_miss_review_dependencies] = lambda: (
            build_default_near_miss_review_dependencies(open_single_tenant_graph=_unreachable)
        )
    else:
        env.app.dependency_overrides[provide_pending_approval_store] = lambda: _FailingStore(
            failure
        )
    before = env.snapshot()

    response = TestClient(env.app, raise_server_exceptions=False).post(
        f"/near-misses/{_REVIEW}/resolve", json={"decision": "merge"}
    )

    assert response.status_code == 500
    assert response.json()["error"]["message"] == "An internal error occurred."
    for leak in _LEAKS:
        assert leak not in response.text
    assert approval_row_count(env.store) == 0
    assert env.snapshot() == before
