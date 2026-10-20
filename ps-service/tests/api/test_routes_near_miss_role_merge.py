"""The near-miss merge routes on a `Role` review: `merge-roles` behind one shared gate.

`POST /near-misses/{id}/resolve` (merge) and `GET /near-misses/approvals/{id}` over a `CleanupRig`
(real reader, planner, executor and gateway rig; only the stores and the signing ceremony are
doubles). The `ComplianceOfficer` check is the one the Capability branch uses, the response
`preview` admits a Role preview, a governance acknowledgment is refused for a Role, and the status
route reports the winner and the loser through the mapper the MCP tool also uses.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

import pytest
from authz._fakes import FakeAccessRoleStore
from fastapi.testclient import TestClient
from graph_cleanup._fakes import (
    ROLE_ABSORBED,
    ROLE_SURVIVOR,
    FakeApprovalStore,
    approval_row_count,
)
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
from ps_service.graph_cleanup.executors import execute_role_merge
from ps_service.logging import configure
from ps_service.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

_REVIEW = "review_role_aaa"
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
    """A rig with two Roles and a `Role` review (incoming: `ROLE_SURVIVOR`), routes wired to it."""

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
        rig.seed_role_merge_world()
        rig.seed_near_miss_review(
            _REVIEW, incoming_id=ROLE_SURVIVOR, existing_id=ROLE_ABSORBED, kind="Role"
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

    def snapshot(self) -> object:
        return copy.deepcopy(dict(self.rig.graph.nodes)), set(self.rig.graph.edges)

    def merge(self, *, acknowledge: bool | None = None) -> tuple[int, dict[str, object]]:
        body: dict[str, object] = {"decision": "merge"}
        if acknowledge is not None:
            body["acknowledge_governance_change"] = acknowledge
        response = self.client.post(f"/near-misses/{_REVIEW}/resolve", json=body)
        return response.status_code, response.json()

    def sign_and_execute(self, approval_id: str) -> dict[str, object]:
        """Sign the way the router does: mark signed, run the executor, store the outcome."""
        assert self.store.mark_signed(approval_id)
        signed = self.store.get_by_id(approval_id)
        assert signed is not None
        outcome = execute_role_merge(signed, CONFIG, self.rig.dependencies(roles()))
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


def test_a_compliance_officer_gets_the_role_preview_and_an_approval_link(env: _Env) -> None:
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
    assert preview["survivor_id"] == ROLE_SURVIVOR  # pyright: ignore[reportIndexIssue]
    assert preview["absorbed_id"] == ROLE_ABSORBED  # pyright: ignore[reportIndexIssue]
    assert preview["state_digest"]  # pyright: ignore[reportIndexIssue]
    row = env.store.get_by_id(str(body["pending_approval_id"]))
    assert row is not None
    assert row.tool_name == "merge-roles"
    assert row.normalized_args["review_id"] == _REVIEW
    assert (row.actor_subject, row.actor_issuer) == OFFICER
    assert env.snapshot() == before
    assert env.audit.rows == []


def test_a_governance_acknowledgment_is_refused_with_409_for_a_role_review(env: _Env) -> None:
    before = env.snapshot()

    status, body = env.merge(acknowledge=True)

    assert status == 409
    assert "acknowledge_governance_change" in body["error"]["message"]  # pyright: ignore[reportIndexIssue]
    assert approval_row_count(env.store) == 0
    assert env.snapshot() == before


def test_the_status_route_reports_the_winner_and_the_loser_of_a_signed_role_merge(
    env: _Env,
) -> None:
    _, merged = env.merge()
    approval_id = str(merged["pending_approval_id"])
    env.sign_and_execute(approval_id)

    response = env.client.get(f"/near-misses/approvals/{approval_id}")

    assert response.status_code == 200
    assert response.json() == {
        "pending_approval_id": approval_id,
        "status": "signed",
        "review_id": _REVIEW,
        "decision": "merge",
        "winner_id": ROLE_SURVIVOR,
        "loser_id": ROLE_ABSORBED,
        "error": None,
    }
    assert ("MergedRole", ROLE_ABSORBED) in env.rig.graph.nodes
    assert ("PendingReview", _REVIEW) not in env.rig.graph.nodes
