"""Declarative role gates for MCP tools.

A tool is registered through `gated_tool`, which records its `ToolGate` declaration in
`TOOL_GATES` and, for a `ROLE` gate, wraps the tool so the role check runs once, before the tool
body. The wrapper resolves the caller, applies the declared local-test-bypass policy, runs the
request gate (`ps_service.authz.request_gate.enforce_role_gate`, which writes the `access.gate`
audit row on a denial) and turns a denial into the tool's usual `error: ...` return value, logged
through the same started/failed pair as any other handled tool failure.

This module cannot import `ps_service.mcp_interface.mcp_server` (the server imports this module),
so the server passes a `GateEnvironment` of callables that look up its configuration loader, caller
resolvers, store constructors and action runner at call time.

Public names: `GateKind`, `BypassPolicy`, `ToolGate`, `GateEnvironment`, `TOOL_GATES`, `gated_tool`,
`run_body_gate`, `lifecycle_role_enforcer`.
"""

from __future__ import annotations

import dataclasses
import functools
import inspect
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ps_service.api.errors import AccessDeniedError, AuthorizationStoreUnavailableError
from ps_service.audit.actor import resolve_audit_actor
from ps_service.authz.request_gate import RoleGate, enforce_role_gate

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from ps_service.audit.store import AuditStore
    from ps_service.authz.models import AccessRole
    from ps_service.authz.store import AccessRoleStore
    from ps_service.config import ServiceConfig

type ToolResult = dict[str, object] | str


class GateKind(StrEnum):
    """How a tool's access is controlled.

    Members: `ROLE` (the wrapper runs the role gate before the tool body), `BODY` (the tool body
    runs the role gate for one branch), `AUTHENTICATED_ONLY` (any authenticated caller; needs a
    `reason`), `SELF_AUDITED` (the tool's service layer enforces access and writes its own denial
    row; needs a `reason`).
    """

    ROLE = "role"
    BODY = "body"
    AUTHENTICATED_ONLY = "authenticated_only"
    SELF_AUDITED = "self_audited"


class BypassPolicy(StrEnum):
    """What a gate does under the local-test authentication bypass.

    Members: `SKIP` (the gate is not run), `REFUSE` (no verified identity exists under the bypass,
    so the call is refused with the unauthenticated message), `LIFECYCLE_IDENTITY` (the role check
    runs for the policy-lifecycle fallback identity), `NOT_APPLICABLE` (no role gate).
    """

    SKIP = "skip"
    REFUSE = "refuse"
    LIFECYCLE_IDENTITY = "lifecycle_identity"
    NOT_APPLICABLE = "not_applicable"


@dataclasses.dataclass(frozen=True, slots=True)
class ToolGate:
    """One tool's access declaration.

    Attributes:
        kind: How access is controlled.
        role: The minimum `AccessRole`; required for `ROLE` and `BODY`, absent otherwise.
        bypass: The local-test-bypass policy; `NOT_APPLICABLE` when there is no role gate.
        unauthenticated_message: The `error: ...` string returned when no verified caller is
            bound; required for `ROLE` and `BODY`.
        reason: Why the tool needs no role gate; required for `AUTHENTICATED_ONLY` and
            `SELF_AUDITED`.

    Raises:
        ValueError: at construction when the fields do not fit `kind`.
    """

    kind: GateKind
    role: AccessRole | None = None
    bypass: BypassPolicy = BypassPolicy.NOT_APPLICABLE
    unauthenticated_message: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        """Reject a declaration whose fields do not match its kind."""
        has_role_gate = self.kind in {GateKind.ROLE, GateKind.BODY}
        if has_role_gate != (self.role is not None):
            message = f"a {self.kind} gate {'requires' if has_role_gate else 'forbids'} a role"
            raise ValueError(message)
        if has_role_gate and self.unauthenticated_message is None:
            message = f"a {self.kind} gate requires an unauthenticated_message"
            raise ValueError(message)
        needs_reason = self.kind in {GateKind.AUTHENTICATED_ONLY, GateKind.SELF_AUDITED}
        if needs_reason and not self.reason:
            message = f"a {self.kind} gate requires a reason"
            raise ValueError(message)


@dataclasses.dataclass(frozen=True, slots=True)
class GateEnvironment:
    """The server-side collaborators a gated tool wrapper needs, supplied as callables.

    Each callable is looked up when it is called, so a module-level name the server rebinds (a
    store class, the configuration loader) is honoured.

    Attributes:
        load_config: Returns the service configuration.
        resolve_actor: Returns the verified `(sub, iss)` pair, or `None`.
        role_store_factory: Builds the access-role store for a configuration.
        audit_store_factory: Builds the audit store for a configuration.
        run_action: Runs a tool body inside the run-id-correlated started/terminal log pair.
    """

    load_config: Callable[[], ServiceConfig]
    resolve_actor: Callable[[ServiceConfig], tuple[str, str] | None]
    role_store_factory: Callable[[ServiceConfig], AccessRoleStore]
    audit_store_factory: Callable[[ServiceConfig], AuditStore]
    run_action: Callable[[str, Callable[[], ToolResult]], ToolResult]


TOOL_GATES: dict[str, ToolGate] = {}
"""Every registered tool's declaration, keyed by registered tool name."""


@dataclasses.dataclass(frozen=True, slots=True)
class _GateOutcome:
    """The result of running a gate: a refusal `message`, or `None` plus the admitted `actor`."""

    message: str | None
    actor: tuple[str, str] | None


def _check_gate(environment: GateEnvironment, name: str, gate: ToolGate) -> _GateOutcome:
    """Run `gate` for the current caller; a `None` message in the outcome means allowed."""
    config = environment.load_config()
    if gate.bypass is BypassPolicy.SKIP and config.is_local_test_bypass_active:
        return _GateOutcome(None, None)
    actor = environment.resolve_actor(config)
    if actor is None:
        return _GateOutcome(gate.unauthenticated_message, None)
    assert gate.role is not None  # noqa: S101  # guaranteed by ToolGate.__post_init__ for ROLE and BODY
    try:
        enforce_role_gate(
            RoleGate(operation=name, minimum=gate.role),
            principal=actor,
            audit_actor=actor,
            role_store=environment.role_store_factory(config),
            audit_store=environment.audit_store_factory(config),
        )
    except (AccessDeniedError, AuthorizationStoreUnavailableError) as exc:
        return _GateOutcome(f"error: {exc}", actor)
    return _GateOutcome(None, actor)


def run_body_gate(environment: GateEnvironment, name: str) -> tuple[str, str] | str:
    """Run the `BODY` gate declared for tool `name`, from inside the tool body.

    For a tool whose role requirement applies to one branch only. It applies the same bypass
    policy, request gate and denial wording as the wrapper does for a `ROLE` tool.

    Args:
        environment: The server-side collaborators.
        name: The registered tool name; its declaration must be a `BODY` gate.

    Returns:
        The admitted caller's verified `(sub, iss)`, or the `error: ...` string to return as is.

    Raises:
        ValueError: `name` has no `BODY` declaration.
    """
    gate = TOOL_GATES.get(name)
    if gate is None or gate.kind is not GateKind.BODY:
        message = f"tool {name!r} has no body gate declaration"
        raise ValueError(message)
    outcome = _check_gate(environment, name, gate)
    if outcome.message is not None:
        return outcome.message
    assert outcome.actor is not None  # noqa: S101  # a BODY gate never skips the bypass
    return outcome.actor


def lifecycle_role_enforcer(
    environment: GateEnvironment, name: str
) -> Callable[[tuple[str, str]], None]:
    """Return the role enforcer that lifecycle tool `name` hands its service.

    The policy-lifecycle service runs the role check itself, after it has read the Policy, so the
    check order and responses stay as they were. The service receives this callable and calls it
    with the caller identity it resolved; the call runs the request gate for that identity and
    raises on a denial. Under the local-test bypass the role is checked for that fallback
    identity, but the `access.gate` row is attributed to the bypass sentinel, never to the
    fallback identity.

    Args:
        environment: The server-side collaborators.
        name: The registered tool name; its declaration must be a `BODY` gate with the
            `LIFECYCLE_IDENTITY` bypass policy.

    Returns:
        A callable taking the identity whose role is checked.

    Raises:
        ValueError: `name` has no such declaration.
    """
    gate = TOOL_GATES.get(name)
    if gate is None or gate.kind is not GateKind.BODY or gate.role is None:
        message = f"tool {name!r} has no body gate declaration"
        raise ValueError(message)
    if gate.bypass is not BypassPolicy.LIFECYCLE_IDENTITY:
        message = f"tool {name!r} does not use the lifecycle identity"
        raise ValueError(message)
    minimum = gate.role

    def _enforce(principal: tuple[str, str]) -> None:
        config = environment.load_config()
        audit_actor = resolve_audit_actor(
            environment.resolve_actor(config),
            is_local_test_bypass_active=config.is_local_test_bypass_active,
        )
        enforce_role_gate(
            RoleGate(operation=name, minimum=minimum),
            principal=principal,
            audit_actor=audit_actor,
            role_store=environment.role_store_factory(config),
            audit_store=environment.audit_store_factory(config),
        )

    return _enforce


def _wrap(
    environment: GateEnvironment, name: str, gate: ToolGate, tool: Callable[..., Any]
) -> Callable[..., Any]:
    """Wrap `tool` so `gate` runs before it; a refusal is returned, not raised."""
    log_action = name.replace("-", "_")

    def _refusal(message: str) -> ToolResult:
        return environment.run_action(log_action, lambda: message)

    if inspect.iscoroutinefunction(tool):

        @functools.wraps(tool)
        async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            outcome = _check_gate(environment, name, gate)
            if outcome.message is not None:
                return _refusal(outcome.message)
            return await tool(*args, **kwargs)

        return _async_wrapper

    @functools.wraps(tool)
    def _wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        outcome = _check_gate(environment, name, gate)
        if outcome.message is not None:
            return _refusal(outcome.message)
        return tool(*args, **kwargs)

    return _wrapper


def gated_tool(
    server: MCPServer, environment: GateEnvironment
) -> Callable[[str, ToolGate], Callable[[Callable[..., Any]], Callable[..., Any]]]:
    """Return the registration decorator factory for `server`.

    Usage: `@gated_tool(server, environment)("tool-name", ToolGate(...))`. The declaration is
    recorded in `TOOL_GATES` for every kind; a `ROLE` tool is additionally wrapped so the role gate
    runs before its body. The decorated name stays bound to the wrapped callable, so a direct call
    is gated too.

    Args:
        server: The MCP server the tool is registered on.
        environment: The server-side collaborators the wrapper uses.

    Returns:
        A factory taking the registered tool name and its `ToolGate` and returning the decorator.

    Raises:
        ValueError: at registration when the tool name already has a declaration.
    """

    def _factory(name: str, gate: ToolGate) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def _decorate(tool: Callable[..., Any]) -> Callable[..., Any]:
            if name in TOOL_GATES:
                message = f"tool {name!r} already has a gate declaration"
                raise ValueError(message)
            TOOL_GATES[name] = gate
            registered = (
                _wrap(environment, name, gate, tool) if gate.kind is GateKind.ROLE else tool
            )
            return server.tool(name=name)(registered)

        return _decorate

    return _factory


__all__ = [
    "TOOL_GATES",
    "BypassPolicy",
    "GateEnvironment",
    "GateKind",
    "ToolGate",
    "gated_tool",
    "lifecycle_role_enforcer",
    "run_body_gate",
]
