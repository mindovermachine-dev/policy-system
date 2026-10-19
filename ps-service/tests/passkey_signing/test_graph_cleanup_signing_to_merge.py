"""Signing ceremony to merge execution over HTTP.

The real approval service, signing router, real WebAuthn verification, executor registry,
executor, planner and gateway seam run; only the graph (the gateway rig's in-memory log and
FalkorDB fakes), the stores and the role grants are fakes. A replayed, expired or re-targeted
approval never reaches the graph. The merge is one gateway group.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import TYPE_CHECKING, cast

import pytest
from authz._fakes import FakeAccessRoleStore
from fastapi.testclient import TestClient
from graph_cleanup._fakes import (
    ABSORBED,
    SURVIVOR,
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
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.service import (
    check_cleanup_approval,
    create_capability_merge_approval,
)
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


class _World:
    def __init__(self) -> None:
        self.rig = CleanupRig()
        self.rig.seed_capability_merge_world()
        self.graph = self.rig.read
        self.audit = self.rig.audit
        self.pending = FakeApprovalStore()
        self.credentials = FakeSigningCredentialStore()
        self.roles = FakeAccessRoleStore(expected_owner=_OWNER)
        self.roles.bootstrap_first_owner(_OWNER)
        self.roles.grant(actor=_OWNER, target=_ACTOR, access_role=AccessRole.COMPLIANCE_OFFICER)
        self.authenticator = Authenticator()
        enroll(self.credentials, self.authenticator)
        app = create_app(
            ServiceConfig(
                host="127.0.0.1",
                port=8000,
                graceful_shutdown_seconds=10,
                logging_dir=None,
                is_local_test_bypass_active=True,
                authentik_api_token="t",
                authentik_base_url="https://authentik.example.com",
            )
        )
        app.dependency_overrides[provide_pending_approval_store] = lambda: self.pending
        app.dependency_overrides[provide_signing_credential_store] = lambda: self.credentials
        self.client = TestClient(app)

    def fail_graph_write(self, error: Exception) -> None:
        """Make the gateway fail the group's append (the write the executor maps to `failed`)."""
        self.rig.store.fail_appends(error=GraphLogPersistenceError(str(error)))

    def approve(self, *, acknowledge: bool = False) -> tuple[PendingApprovalRow, str]:
        approval = create_capability_merge_approval(
            self.graph,
            survivor_id=SURVIVOR,
            absorbed_id=ABSORBED,
            acknowledge_governance_change=acknowledge,
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


def test_importing_the_app_registers_the_merge_executor_and_verifier() -> None:
    from ps_service.passkey_signing.executors import (
        resolve_approval_executor,
        resolve_effect_verifier,
    )

    assert resolve_approval_executor("merge-capabilities") is not None
    assert resolve_effect_verifier("merge-capabilities") is not None


def test_a_valid_signature_audits_then_writes_once_and_the_outcome_is_checkable(
    world: _World,
) -> None:
    row, code = world.approve()

    response = world.sign(row, code)

    assert response.status_code == 200
    assert response.json() == {
        "status": "signed",
        "survivor_id": SURVIVOR,
        "absorbed_id": ABSORBED,
        "merged": True,
    }
    assert world.rig.order() == ["audit:applied", "log_append"]
    assert len(world.rig.logged_groups) == 1
    status = check_cleanup_approval(
        pending_approval_id=row.id,
        actor=_ACTOR,
        store=world.pending,
        config=ServiceConfig(
            host="127.0.0.1", port=8000, graceful_shutdown_seconds=10, logging_dir=None
        ),
        dependencies=GraphCleanupDependencies(open_single_tenant_graph=lambda _c: world.graph),
    )
    assert status is not None
    assert status.status == "signed"
    assert status.outcome == {"survivor_id": SURVIVOR, "absorbed_id": ABSORBED, "merged": True}


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
        {**row.normalized_args, "survivor_id": ABSORBED, "absorbed_id": SURVIVOR},
    )

    # The assertion signs the challenge of the pair that was approved; the stored pair differs.
    response = world.sign(row, code)

    assert response.status_code == 404
    assert world.rig.logged_groups == []
    assert world.audit.rows == []


def test_a_signed_case_two_approval_executes_with_the_acknowledgment_audited(
    world: _World,
) -> None:
    world.rig.seed_policy("pol_1", title="Incident Policy", status="approved")
    world.rig.seed_governs(ABSORBED, "pol_1")
    world.rig.seed_governs("cap_other", "pol_1")
    row, code = world.approve(acknowledge=True)

    response = world.sign(row, code)

    assert response.status_code == 200
    assert response.json()["merged"] is True
    assert world.rig.order() == ["audit:applied", "log_append"]
    [recorded] = world.audit.rows
    assert recorded.details["policy_case"] == 2
    assert recorded.details["acknowledged"] is True
    assert recorded.details["policy_status"] == "approved"


def test_a_case_two_row_forged_without_the_acknowledgment_is_refused_at_signing(
    world: _World,
) -> None:
    world.rig.seed_policy("pol_1", title="Incident Policy", status="approved")
    world.rig.seed_governs(ABSORBED, "pol_1")
    world.rig.seed_governs("cap_other", "pol_1")
    row, code = world.approve(acknowledge=True)
    tamper_approval_args(
        world.pending, row.id, {**row.normalized_args, "acknowledge_governance_change": False}
    )

    response = world.sign(row, code)

    assert response.status_code == 404  # the signed challenge covered the acknowledged args
    assert world.rig.logged_groups == []
    assert world.audit.rows == []


# --- Signing leg: fixed outcome, no leak, no partial write -------------------------

_LEAKS = ("10.1.2.3", "6379", "hunter2", "/secret/path")


def _graph_state(world: _World) -> object:
    return copy.deepcopy(dict(world.rig.graph.nodes)), set(world.rig.graph.edges)


def test_an_unreachable_graph_at_signing_gives_a_fixed_outcome_and_a_failed_row(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, code = world.approve()
    before = _graph_state(world)

    def _unreachable(_config: object) -> object:
        message = "connection refused to 10.1.2.3:6379 password=hunter2 at /secret/path"
        raise ConnectionError(message)

    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
        _factory(_unreachable),
    )

    response = world.sign(row, code)

    assert response.status_code == 200
    assert response.json() == {
        "status": "signed",
        "error": "the policy graph database could not be reached; nothing was changed",
    }
    for leak in _LEAKS:
        assert leak not in response.text
    assert _graph_state(world) == before
    assert world.rig.logged_groups == []
    [recorded] = world.audit.rows
    assert recorded.outcome == "failed"
    assert recorded.details["reason_code"] == "graph_unavailable"


def test_an_unexpected_executor_failure_at_signing_gives_a_fixed_outcome_and_no_write(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _ExplodingRoles:
        def __getattr__(self, name: str) -> object:
            message = "boom at /secret/path host 10.1.2.3:6379 password=hunter2"
            raise RuntimeError(message)

    row, code = world.approve()
    before = _graph_state(world)
    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.PsycopgAccessRoleStore",
        _factory(_ExplodingRoles()),
    )

    response = world.sign(row, code)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "signed"
    assert set(body) == {"status", "error"}
    for leak in _LEAKS:
        assert leak not in response.text
    stored = world.pending.get_by_id(row.id)
    assert stored is not None
    for leak in _LEAKS:
        assert leak not in str(stored.outcome)
    assert _graph_state(world) == before
    assert world.rig.logged_groups == []
