"""The REST role gate: `require_access_role(role, operation=...)` audits a denial.

Lives under `tests/authz/` because it needs `authz._fakes` and the importlib collection order puts
`tests/authz` after the packages it imports (`api`, `audit`). It tests `api/dependencies.py`, a
deliberate exception to the one-to-one `tests/` to `src/` mirror.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

import pytest
from api._fakes import (
    build_fake_change_check_dependencies,
    compliance_officer_principal,
    install_compliance_officer_grant,
    install_no_principal,
)
from api._gateway_fakes import process_gateway_installed
from audit._fakes import InMemoryAuditStore
from fastapi.testclient import TestClient

import ps_service.authz.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers `access.gate` when this file runs alone
from authz._fakes import FakeAccessRoleStore
from ps_service.api.dependencies import (
    provide_audit_store,
    provide_change_check_dependencies,
    require_access_role,
)
from ps_service.audit import AuditPostgresUnavailableError
from ps_service.audit.models import AuditQueryFilters
from ps_service.authz.models import AccessRole
from ps_service.authz.request_gate import RoleGate, enforce_role_gate
from ps_service.authz.service import list_audit_events
from ps_service.config import ServiceConfig
from ps_service.main import create_app

if TYPE_CHECKING:
    from api._fakes import FakeChangeCheckDependencies

_APP_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=True,
    authentik_api_token="test-authentik-token",
    authentik_base_url="https://authentik.example.com",
)
_ISSUER = "https://issuer.example.com/"
_DENIED_MESSAGE = "You do not have the required access role for this action."


def _noop_emit(**_kwargs: object) -> None:
    """Discard a sweep log entry (the sweep needs a configured facade otherwise)."""


@pytest.fixture(autouse=True)
def _stub_sweep_log(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]  # autouse
    # detroit-exception: process-wide atexit logging facade, not a business fake
    monkeypatch.setattr("ps_service.api.change_check_orchestration.emit_log_entry", _noop_emit)


def _client(store: InMemoryAuditStore) -> TestClient:
    fake: FakeChangeCheckDependencies = build_fake_change_check_dependencies(tracked=())
    app = create_app(_APP_CONFIG)
    app.dependency_overrides[provide_change_check_dependencies] = lambda: fake.dependencies
    app.dependency_overrides[provide_audit_store] = lambda: store
    return TestClient(app, raise_server_exceptions=False)


def test_denied_change_check_returns_403_with_the_unchanged_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_compliance_officer_grant(monkeypatch, granted=False)
    response = _client(InMemoryAuditStore()).post("/change-checks")
    assert response.status_code == 403
    error = response.json()["error"]
    assert (error["code"], error["message"]) == ("access_denied", _DENIED_MESSAGE)


def test_denied_change_check_writes_one_access_gate_row_named_after_method_and_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_compliance_officer_grant(monkeypatch, granted=False)
    store = InMemoryAuditStore()
    _client(store).post("/change-checks")
    (row,) = store.rows
    principal = compliance_officer_principal()
    assert (row.actor_subject, row.actor_issuer) == (principal.sub, principal.iss)
    assert (row.action, row.outcome) == ("access.gate", "rejected")
    assert (row.resource_type, row.resource_id) == ("operation", "POST /change-checks")
    assert row.details == {"required_role": "ComplianceOfficer"}


@pytest.mark.parametrize(
    "path",
    ["/ingestions", "/restorations", "/restorations/from-catalog", "/exports", "/change-checks"],
)
def test_every_gated_rest_route_writes_a_row_on_denial(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_compliance_officer_grant(monkeypatch, granted=False)
    store = InMemoryAuditStore()
    response = _client(store).post(path)
    assert response.status_code == 403
    (row,) = store.rows
    assert (row.action, row.outcome, row.resource_type) == ("access.gate", "rejected", "operation")
    assert row.resource_id == f"POST {path}"
    assert row.details == {"required_role": "ComplianceOfficer"}


def test_denial_survives_an_unwritable_audit_store_over_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_compliance_officer_grant(monkeypatch, granted=False)
    store = InMemoryAuditStore(
        fail_on_action={"access.gate": AuditPostgresUnavailableError("host=db.internal")}
    )
    response = _client(store).post("/change-checks")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "access_denied"
    assert store.rows == []


def test_request_without_a_verified_principal_is_denied_and_writes_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_no_principal(monkeypatch)
    store = InMemoryAuditStore()
    response = _client(store).post("/change-checks")
    assert response.status_code == 403
    assert store.rows == []


def test_permitted_change_check_writes_no_access_gate_row(monkeypatch: pytest.MonkeyPatch) -> None:
    install_compliance_officer_grant(monkeypatch, granted=True)
    store = InMemoryAuditStore()
    with process_gateway_installed():
        response = _client(store).post("/change-checks")
    assert response.status_code == 200, response.text
    assert [row for row in store.rows if row.action == "access.gate"] == []


def test_require_access_role_requires_the_operation_keyword() -> None:
    with pytest.raises(TypeError):
        inspect.signature(require_access_role).bind(AccessRole.COMPLIANCE_OFFICER)


def test_role_gate_dependency_exposes_its_gate_for_the_structural_test() -> None:
    dependency = require_access_role(AccessRole.COMPLIANCE_OFFICER, operation="POST /change-checks")
    assert dependency.gate == RoleGate(
        operation="POST /change-checks", minimum=AccessRole.COMPLIANCE_OFFICER
    )


def test_rest_denial_row_has_the_same_shape_as_the_mcp_denial_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_compliance_officer_grant(monkeypatch, granted=False)
    rest_store = InMemoryAuditStore()
    _client(rest_store).post("/change-checks")
    mcp_store = InMemoryAuditStore()
    roles = FakeAccessRoleStore(expected_owner=("owner", _ISSUER))
    roles.bootstrap_first_owner(("owner", _ISSUER))
    with pytest.raises(Exception, match="required access role"):
        enforce_role_gate(
            RoleGate(operation="set-catalog-source", minimum=AccessRole.COMPLIANCE_OFFICER),
            principal=("caller", _ISSUER),
            audit_actor=("caller", _ISSUER),
            role_store=roles,
            audit_store=mcp_store,
        )

    def shape(store: InMemoryAuditStore) -> tuple[object, ...]:
        (row,) = store.rows
        return (row.action, row.outcome, row.resource_type, tuple(row.details))

    assert shape(rest_store) == shape(mcp_store)


def test_denied_change_check_row_is_returned_by_list_audit_events_for_actor_and_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_compliance_officer_grant(monkeypatch, granted=False)
    store = InMemoryAuditStore()
    _client(store).post("/change-checks")
    owner = ("owner", _ISSUER)
    roles = FakeAccessRoleStore(expected_owner=owner)
    roles.bootstrap_first_owner(owner)
    principal = compliance_officer_principal()
    page = list_audit_events(
        owner,
        filters=AuditQueryFilters(actor_subject=principal.sub, action="access.gate"),
        cursor=None,
        page_size=10,
        access_role_store=roles,
        audit_store=store,
    )
    assert [(e.actor_subject, e.action, e.resource_id) for e in page.events] == [
        (principal.sub, "access.gate", "POST /change-checks")
    ]
