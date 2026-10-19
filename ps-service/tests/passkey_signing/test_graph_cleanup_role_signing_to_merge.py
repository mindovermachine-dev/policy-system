"""Signing ceremony to role-merge execution over HTTP.

The real approval service, signing router, real WebAuthn verification,
executor registry, executor, planner and gateway seam run; only the graph (the gateway rig's
in-memory log and FalkorDB fakes), the stores and the role grants are fakes. A replayed, expired
or re-targeted approval never reaches the graph. The merge is one gateway group.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, cast

import pytest
from authz._fakes import FakeAccessRoleStore
from fastapi.testclient import TestClient
from graph_cleanup._fakes import (
    ROLE_ABSORBED,
    ROLE_SURVIVOR,
    FakeApprovalStore,
    expire_approval,
    tamper_approval_args,
)
from graph_cleanup._gateway_fakes import CleanupRig
from graph_gateway._fakes import close_all_rigs

from passkey_signing._fakes import FakeSigningCredentialStore
from passkey_signing._harness import (
    ACTOR_ISSUER,
    ACTOR_SUBJECT,
    ORIGIN,
    RP_ID,
    Authenticator,
    enroll,
    sign_challenge,
)
from ps_service.api.dependencies import provide_pending_approval_store
from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.service import check_cleanup_approval, create_role_merge_approval
from ps_service.graph_gateway.errors import GraphLogPersistenceError
from ps_service.logging import configure
from ps_service.main import create_app
from ps_service.passkey_signing.router import provide_signing_credential_store

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from ps_service.graph_gateway.system_audit import SystemAuditRecorder
    from ps_service.passkey_signing.models import PendingApprovalRow

_OWNER = ("owner", "https://issuer.example.com/")
_ACTOR = (ACTOR_SUBJECT, ACTOR_ISSUER)
_APP_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=True,
    authentik_api_token="t",
    authentik_base_url="https://authentik.example.com",
)
_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=False,
)


class _World:
    def __init__(self) -> None:
        self.rig = CleanupRig()
        self.rig.seed_role_merge_world()
        self.graph = self.rig.read
        self.audit = self.rig.audit
        self.pending = FakeApprovalStore()
        self.credentials = FakeSigningCredentialStore()
        self.roles = FakeAccessRoleStore(expected_owner=_OWNER)
        self.roles.bootstrap_first_owner(_OWNER)
        self.roles.grant(actor=_OWNER, target=_ACTOR, access_role=AccessRole.COMPLIANCE_OFFICER)
        self.authenticator = Authenticator()
        enroll(self.credentials, self.authenticator)
        app = create_app(_APP_CONFIG)
        app.dependency_overrides[provide_pending_approval_store] = lambda: self.pending
        app.dependency_overrides[provide_signing_credential_store] = lambda: self.credentials
        self.client = TestClient(app)

    def fail_graph_write(self, error: Exception) -> None:
        """Make the gateway fail the group's append (the write the executor maps to `failed`)."""
        self.rig.store.fail_appends(error=GraphLogPersistenceError(str(error)))

    def approve(self) -> tuple[PendingApprovalRow, str]:
        approval = create_role_merge_approval(
            self.graph,
            survivor_id=ROLE_SURVIVOR,
            absorbed_id=ROLE_ABSORBED,
            actor=_ACTOR,
            base_url="https://ps.example.com",
            store=self.pending,
        )
        row = self.pending.get_by_id(approval.pending_approval_id)
        assert row is not None
        return row, approval.approval_url.rsplit("#", 1)[1]

    def sign(self, row: PendingApprovalRow, code: str, *, sign_count: int = 1):
        assertion = self.authenticator.build_assertion(
            rp_id=RP_ID, origin=ORIGIN, challenge=sign_challenge(row), sign_count=sign_count
        )
        return self.client.post(
            f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": assertion}
        )


def _factory(value: object) -> Callable[..., object]:
    def _make(*_args: object, **_kwargs: object) -> object:
        return value

    return _make


def _opener_for(graph: object) -> Callable[[object], object]:
    def _open(_config: object) -> object:
        return graph

    return _open


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    configure()
    built = _World()
    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
        _factory(_opener_for(built.graph)),
    )
    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.PsycopgAuditStore", _factory(built.audit)
    )
    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.PsycopgAccessRoleStore", _factory(built.roles)
    )
    install_graph_write_gateway(
        built.rig.gateway, system_audit=lambda _config: cast("SystemAuditRecorder", object())
    )
    yield built
    install_graph_write_gateway(None)
    close_all_rigs()


def test_importing_the_app_registers_the_role_executor_and_verifier() -> None:
    from ps_service.passkey_signing.executors import (
        resolve_approval_executor,
        resolve_effect_verifier,
    )

    assert resolve_approval_executor("merge-roles") is not None
    assert resolve_effect_verifier("merge-roles") is not None


def test_a_valid_signature_audits_then_writes_once(world: _World) -> None:
    row, code = world.approve()

    response = world.sign(row, code)

    assert response.status_code == 200
    assert response.json() == {
        "status": "signed",
        "survivor_id": ROLE_SURVIVOR,
        "absorbed_id": ROLE_ABSORBED,
        "merged": True,
    }
    assert world.rig.order() == ["audit:applied", "log_append"]
    assert len(world.rig.logged_groups) == 1
    [recorded] = world.audit.rows
    assert recorded.action == "role.merge"


def test_replaying_the_signed_request_is_rejected_without_a_second_edit(world: _World) -> None:
    row, code = world.approve()
    assert world.sign(row, code).status_code == 200

    replay = world.sign(row, code, sign_count=2)

    assert replay.status_code == 404
    assert len(world.rig.logged_groups) == 1
    assert len(world.audit.rows) == 1


def test_an_expired_approval_is_rejected_and_never_reaches_the_graph(world: _World) -> None:
    row, code = world.approve()
    expire_approval(world.pending, row.id, expired_for=timedelta(seconds=1))

    response = world.sign(row, code)

    assert response.status_code == 404
    assert world.rig.logged_groups == []
    assert world.audit.rows == []


def test_an_approval_presented_for_a_different_pair_is_rejected(world: _World) -> None:
    row, code = world.approve()
    tamper_approval_args(
        world.pending,
        row.id,
        {**row.normalized_args, "survivor_id": ROLE_ABSORBED, "absorbed_id": ROLE_SURVIVOR},
    )

    response = world.sign(row, code)

    assert response.status_code == 404
    assert world.rig.logged_groups == []
    assert world.audit.rows == []


def test_a_signed_role_merge_moves_the_obligations_and_check_cleanup_approval_reports_it(
    world: _World,
) -> None:
    row, code = world.approve()

    assert world.sign(row, code).status_code == 200

    assert ("Role", ROLE_ABSORBED) not in world.rig.graph.nodes
    assert world.rig.graph.nodes[("MergedRole", ROLE_ABSORBED)]["merged_into"] == ROLE_SURVIVOR
    status = check_cleanup_approval(
        pending_approval_id=row.id,
        actor=_ACTOR,
        store=world.pending,
        config=_CONFIG,
        dependencies=world.rig.dependencies(world.roles),
    )
    assert status is not None
    assert status.tool_name == "merge-roles"
    assert status.status == "signed"
    assert status.outcome == {
        "survivor_id": ROLE_SURVIVOR,
        "absorbed_id": ROLE_ABSORBED,
        "merged": True,
    }
