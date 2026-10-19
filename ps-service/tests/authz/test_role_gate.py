"""Tests for the pure role decision and the request gate (`check_role`, `enforce_role_gate`)."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from audit._fakes import InMemoryAuditStore
from pydantic import ValidationError

import ps_service.authz.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers the gate action when this file runs alone
from authz._fakes import FakeAccessRoleStore, RaisingAccessRoleStore
from ps_service.api.errors import AccessDeniedError, AuthorizationStoreUnavailableError
from ps_service.audit.errors import AuditPostgresUnavailableError
from ps_service.audit.models import is_known_resource_type, resolve_details_model
from ps_service.authz.audit_actions import AccessGateDetails
from ps_service.authz.models import AccessRole
from ps_service.authz.request_gate import RoleGate, enforce_role_gate
from ps_service.authz.service import RoleDecision, check_role

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging.emitter import LogEmitter

    type ReadLines = Callable[[Path], list[dict[str, object]]]
    type MakeEmitter = Callable[..., tuple[LogEmitter, Path]]

_ISSUER = "https://issuer.example.com/"
_OWNER = ("owner-sub", _ISSUER)
_ADMIN = ("admin-sub", _ISSUER)
_CALLER = ("caller-sub", _ISSUER)
_OPERATION = "set-catalog-source"
_DENIED_MESSAGE = "You do not have the required access role for this action."
_GATE = RoleGate(operation=_OPERATION, minimum=AccessRole.SYSTEM_ADMIN)


@dataclass
class _CountingStore(FakeAccessRoleStore):
    """A role store that counts how often the first-owner bootstrap is attempted."""

    bootstrap_calls: int = field(default=0)

    def bootstrap_first_owner(self, principal: tuple[str, str]) -> frozenset[AccessRole]:
        self.bootstrap_calls += 1
        return super().bootstrap_first_owner(principal)


def _seeded_store() -> FakeAccessRoleStore:
    """A store whose bootstrapped owner is `_OWNER`, with `_ADMIN` granted `SystemAdmin`."""
    store = FakeAccessRoleStore(expected_owner=_OWNER)
    store.bootstrap_first_owner(_OWNER)
    store.grant(actor=_OWNER, target=_ADMIN, access_role=AccessRole.SYSTEM_ADMIN)
    return store


def _enforce(
    *,
    principal: tuple[str, str] | None,
    audit_actor: tuple[str, str] | None,
    role_store: FakeAccessRoleStore,
    audit_store: InMemoryAuditStore,
    emitter: LogEmitter | None = None,
) -> None:
    enforce_role_gate(
        _GATE,
        principal=principal,
        audit_actor=audit_actor,
        role_store=role_store,
        audit_store=audit_store,
        emitter=emitter,
    )


def test_check_role_allows_a_principal_holding_the_minimum_role() -> None:
    decision = check_role(_ADMIN, minimum=AccessRole.SYSTEM_ADMIN, store=_seeded_store())
    assert decision == RoleDecision(allowed=True, minimum=AccessRole.SYSTEM_ADMIN)


def test_check_role_lets_a_system_owner_satisfy_a_system_admin_minimum() -> None:
    decision = check_role(_OWNER, minimum=AccessRole.SYSTEM_ADMIN, store=_seeded_store())
    assert decision.allowed is True


def test_check_role_denies_without_raising_and_writes_no_audit_row() -> None:
    audit_store = InMemoryAuditStore()
    for store in (FakeAccessRoleStore(), _seeded_store()):
        decision = check_role(_CALLER, minimum=AccessRole.SYSTEM_ADMIN, store=store)
        assert decision.allowed is False
    assert audit_store.rows == []


def test_check_role_on_an_empty_store_denies_and_does_not_bootstrap_or_write() -> None:
    store = FakeAccessRoleStore(expected_owner=_OWNER)
    audit_store = InMemoryAuditStore()
    decision = check_role(_OWNER, minimum=AccessRole.SYSTEM_OWNER, store=store)
    assert decision.allowed is False
    assert store.active_roles_for(_OWNER) == frozenset()
    assert audit_store.rows == []


def test_check_role_never_calls_bootstrap_first_owner() -> None:
    store = _CountingStore(expected_owner=_OWNER)
    check_role(_OWNER, minimum=AccessRole.SYSTEM_OWNER, store=store)
    check_role(_CALLER, minimum=AccessRole.SYSTEM_OWNER, store=store)
    assert store.bootstrap_calls == 0


def test_check_role_fails_closed_when_the_store_is_unavailable() -> None:
    with pytest.raises(AuthorizationStoreUnavailableError):
        check_role(_ADMIN, minimum=AccessRole.SYSTEM_ADMIN, store=RaisingAccessRoleStore())  # pyright: ignore[reportArgumentType]  # the fake raises on purpose and is not a full store


def test_raise_if_denied_raises_access_denied_with_the_fixed_message() -> None:
    denied = RoleDecision(allowed=False, minimum=AccessRole.SYSTEM_ADMIN)
    with pytest.raises(AccessDeniedError) as raised:
        denied.raise_if_denied()
    assert str(raised.value) == _DENIED_MESSAGE
    RoleDecision(allowed=True, minimum=AccessRole.SYSTEM_ADMIN).raise_if_denied()


def test_denied_request_gate_writes_one_row_with_actor_operation_and_required_role_only() -> None:
    audit_store = InMemoryAuditStore()
    with pytest.raises(AccessDeniedError):
        _enforce(
            principal=_CALLER,
            audit_actor=_CALLER,
            role_store=_seeded_store(),
            audit_store=audit_store,
        )
    (row,) = audit_store.rows
    assert (row.actor_subject, row.actor_issuer) == _CALLER
    assert (row.action, row.outcome) == ("access.gate", "rejected")
    assert (row.resource_type, row.resource_id) == ("operation", _OPERATION)
    assert row.details == {"required_role": "SystemAdmin"}


def test_denied_request_gate_still_raises_access_denied() -> None:
    with pytest.raises(AccessDeniedError) as raised:
        _enforce(
            principal=_CALLER,
            audit_actor=_CALLER,
            role_store=_seeded_store(),
            audit_store=InMemoryAuditStore(),
        )
    assert str(raised.value) == _DENIED_MESSAGE


def test_denied_request_gate_still_raises_when_the_audit_write_fails() -> None:
    audit_store = InMemoryAuditStore(
        fail_on_action={"access.gate": AuditPostgresUnavailableError("host=db.internal")}
    )
    with pytest.raises(AccessDeniedError):
        _enforce(
            principal=_CALLER,
            audit_actor=_CALLER,
            role_store=_seeded_store(),
            audit_store=audit_store,
        )
    assert audit_store.rows == []


def test_audit_write_failure_is_logged_with_the_error_class_name_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    audit_store = InMemoryAuditStore(
        fail_on_action={"access.gate": AuditPostgresUnavailableError("host=db.internal")}
    )
    with pytest.raises(AccessDeniedError):
        _enforce(
            principal=_CALLER,
            audit_actor=_CALLER,
            role_store=_seeded_store(),
            audit_store=audit_store,
            emitter=emitter,
        )
    emitter.flush()
    (line,) = read_lines(log_path)
    assert line["audit_action"] == "access.gate"
    assert line["reason"] == "AuditPostgresUnavailableError"
    assert "db.internal" not in str(line)
    assert _CALLER[0] not in str(line)


def test_unverified_principal_is_denied_writes_no_row_and_logs_no_identifier(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    audit_store = InMemoryAuditStore()
    with pytest.raises(AccessDeniedError):
        _enforce(
            principal=None,
            audit_actor=None,
            role_store=_seeded_store(),
            audit_store=audit_store,
            emitter=emitter,
        )
    emitter.flush()
    assert audit_store.rows == []
    (line,) = read_lines(log_path)
    assert (line["component"], line["action"], line["outcome"]) == (
        "authz",
        "access_gate",
        "rejected",
    )
    assert line["entity_id"] == _OPERATION
    assert _OWNER[0] not in str(line)
    assert _ISSUER not in str(line)


def test_allowed_request_gate_writes_no_row() -> None:
    audit_store = InMemoryAuditStore()
    _enforce(
        principal=_ADMIN, audit_actor=_ADMIN, role_store=_seeded_store(), audit_store=audit_store
    )
    assert audit_store.rows == []


def test_store_outage_in_request_gate_raises_unavailable_and_writes_no_row() -> None:
    audit_store = InMemoryAuditStore()
    with pytest.raises(AuthorizationStoreUnavailableError):
        _enforce(
            principal=_ADMIN,
            audit_actor=_ADMIN,
            role_store=RaisingAccessRoleStore(),  # pyright: ignore[reportArgumentType]  # the fake raises on purpose and is not a full store
            audit_store=audit_store,
        )
    assert audit_store.rows == []


def test_request_gate_cannot_be_called_without_operation_or_audit_store() -> None:
    parameters = inspect.signature(enforce_role_gate).parameters
    keyword_only = [name for name, p in parameters.items() if p.kind is p.KEYWORD_ONLY]
    assert keyword_only == ["principal", "audit_actor", "role_store", "audit_store", "emitter"]
    required = [name for name, p in parameters.items() if p.default is p.empty]
    assert required == ["gate", "principal", "audit_actor", "role_store", "audit_store"]
    with pytest.raises(TypeError):
        inspect.signature(RoleGate).bind(minimum=AccessRole.SYSTEM_ADMIN)


def test_internal_check_role_denial_writes_no_access_gate_row() -> None:
    audit_store = InMemoryAuditStore()
    check_role(_CALLER, minimum=AccessRole.SYSTEM_ADMIN, store=_seeded_store()).allowed  # noqa: B018
    assert [row for row in audit_store.rows if row.action == "access.gate"] == []


def test_access_gate_details_reject_an_undeclared_field() -> None:
    with pytest.raises(ValidationError):
        AccessGateDetails.model_validate({"required_role": "SystemAdmin", "query": "x"})
    store = InMemoryAuditStore()
    with pytest.raises(ValidationError):
        store.record_standalone(
            actor_subject="a",
            actor_issuer="b",
            action="access.gate",
            resource_type="operation",
            resource_id=_OPERATION,
            outcome="rejected",
            details={"required_role": "SystemAdmin", "extra": "x"},
        )


def test_access_gate_is_registered_with_the_operation_resource_type() -> None:
    assert resolve_details_model("access.gate") is AccessGateDetails
    assert is_known_resource_type("operation")


def test_request_gate_bootstraps_the_configured_owner_then_allows() -> None:
    store = FakeAccessRoleStore(expected_owner=_OWNER)
    audit_store = InMemoryAuditStore()
    enforce_role_gate(
        RoleGate(operation=_OPERATION, minimum=AccessRole.SYSTEM_OWNER),
        principal=_OWNER,
        audit_actor=_OWNER,
        role_store=store,
        audit_store=audit_store,
    )
    assert AccessRole.SYSTEM_OWNER in store.active_roles_for(_OWNER)
    assert audit_store.rows == []


def test_request_gate_for_a_non_owner_on_an_empty_store_writes_one_access_gate_row() -> None:
    store = _CountingStore(expected_owner=_OWNER)
    audit_store = InMemoryAuditStore()
    with pytest.raises(AccessDeniedError):
        _enforce(principal=_CALLER, audit_actor=_CALLER, role_store=store, audit_store=audit_store)
    assert store.bootstrap_calls == 1
    assert store.active_roles_for(_CALLER) == frozenset()
    assert [row.action for row in audit_store.rows] == ["access.gate"]


def test_request_gate_records_the_audit_actor_not_the_checked_principal_when_they_differ() -> None:
    audit_store = InMemoryAuditStore()
    sentinel = ("system:local-test-bypass", "system:local-test-bypass")
    with pytest.raises(AccessDeniedError):
        _enforce(
            principal=_CALLER,
            audit_actor=sentinel,
            role_store=_seeded_store(),
            audit_store=audit_store,
        )
    (row,) = audit_store.rows
    assert (row.actor_subject, row.actor_issuer) == sentinel


def test_request_gate_with_no_audit_actor_denies_and_writes_no_row() -> None:
    audit_store = InMemoryAuditStore()
    with pytest.raises(AccessDeniedError):
        _enforce(
            principal=_CALLER,
            audit_actor=None,
            role_store=_seeded_store(),
            audit_store=audit_store,
        )
    assert audit_store.rows == []


def test_require_role_is_no_longer_exported() -> None:
    import ps_service.authz
    import ps_service.authz.service

    assert not hasattr(ps_service.authz, "require_role")
    assert not hasattr(ps_service.authz.service, "require_role")
    assert "require_role" not in ps_service.authz.__all__


def test_service_layer_role_assertions_never_bootstrap_the_first_owner() -> None:
    from ps_service.authz.service import list_assignments

    store = _CountingStore(expected_owner=_OWNER)
    with pytest.raises(AccessDeniedError):
        list_assignments(_OWNER, store=store)
    assert store.bootstrap_calls == 0
