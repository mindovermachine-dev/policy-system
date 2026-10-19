"""The request gate: decide a caller's role, audit a denial and raise.

One enforcement path for every surface (MCP tool wrapper, REST dependency). The pure decision
lives in `ps_service.authz.service.check_role`; this module adds what only a request gate does:
resolve the caller's roles (bootstrapping the first owner), write an `access.gate` audit row on
a denial and raise `AccessDeniedError`. Internal callers that are not request gates use
`check_role` and never reach this module.

The gate cannot be called without an operation name (carried by `RoleGate`) or an audit store.
The denial row is best-effort: an audit outage is logged by error class name and the caller still
receives the denial.

Side Effects:
    Importing this module imports `ps_service.authz.audit_actions`, which registers the
    `access.gate` audit action and the `operation` audit resource type.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

import ps_service.authz.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers `access.gate`
from ps_service.audit import AuditContext, AuditTarget, record_follow_up_row
from ps_service.authz.service import RoleDecision, decide_role, resolve_active_roles
from ps_service.logging.errors import LoggingLifecycleError
from ps_service.logging.facade import emit_log_entry

if TYPE_CHECKING:
    from ps_service.audit.store import AuditStore
    from ps_service.authz.models import AccessRole
    from ps_service.authz.store import AccessRoleStore
    from ps_service.logging.emitter import LogEmitter

_COMPONENT = "authz"
_GATE_ACTION = "access.gate"
_OPERATION_RESOURCE_TYPE = "operation"


@dataclass(frozen=True, slots=True)
class RoleGate:
    """A declared role requirement for one operation.

    Attributes:
        operation: The name the operation is audited under: the registered tool name on MCP, or
            `"<METHOD> <path template>"` on REST.
        minimum: The least `AccessRole` the operation requires.
    """

    operation: str
    minimum: AccessRole


def _log_unverified_denial(operation: str, emitter: LogEmitter | None) -> None:
    """Log a denial of a caller with no verified identity; carries no principal."""
    with contextlib.suppress(LoggingLifecycleError):
        emit_log_entry(
            component=_COMPONENT,
            action="access_gate",
            entity_id=operation,
            outcome="rejected",
            emitter=emitter,
        )


def enforce_role_gate(
    gate: RoleGate,
    *,
    principal: tuple[str, str] | None,
    audit_actor: tuple[str, str] | None,
    role_store: AccessRoleStore,
    audit_store: AuditStore,
    emitter: LogEmitter | None = None,
) -> None:
    """Enforce `gate` for `principal`: allow silently, or audit the denial and raise.

    Business Rules:
        A caller with no verified identity (`principal is None`) is denied with an operator-log
        entry and no audit row, because a row's actor must be attributable. A verified caller
        who lacks the role is denied with exactly one `access.gate` row (outcome `rejected`)
        attributed to `audit_actor`; when `audit_actor` is `None` no row is written. The row is
        best-effort and never changes the caller's result.

    Args:
        gate: The operation name and the minimum role it requires.
        principal: The `(sub, iss)` identity whose roles are checked; `None` when unverified.
        audit_actor: The `(sub, iss)` pair a denial row is attributed to; `None` writes no row.
            Equal to `principal` except where a surface checks a fallback identity.
        role_store: The store the caller's roles are resolved from.
        audit_store: The store the denial row is written to.
        emitter: Log emitter for operator-log entries; `None` uses the process default.

    Returns:
        None. Returning normally means the caller holds the required role.

    Raises:
        AccessDeniedError: the caller is unverified or lacks the required role.
        AuthorizationStoreUnavailableError: the role store is unavailable; no row is written
            because the role could not be decided.

    Side Effects:
        May bootstrap the first owner (see `resolve_active_roles`). On a denial of a verified
        caller, writes one `access.gate` audit row. On a denial of an unverified caller, emits
        one operator-log entry.
    """
    if principal is None:
        _log_unverified_denial(gate.operation, emitter)
        RoleDecision(allowed=False, minimum=gate.minimum).raise_if_denied()
        return
    decision = decide_role(resolve_active_roles(principal, store=role_store), gate.minimum)
    if not decision.allowed and audit_actor is not None:
        record_follow_up_row(
            AuditContext(audit_actor, audit_store),
            AuditTarget(
                action=_GATE_ACTION,
                resource_type=_OPERATION_RESOURCE_TYPE,
                resource_id=gate.operation,
            ),
            component=_COMPONENT,
            outcome="rejected",
            details={"required_role": str(gate.minimum)},
            emitter=emitter,
        )
    decision.raise_if_denied()
