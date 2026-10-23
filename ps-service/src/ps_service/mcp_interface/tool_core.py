"""Shared plumbing of the PS MCP tools: the one `MCPServer`, the gates and the patch seam.

Defines `server`, the single `MCPServer` instance on which every PS tool and the
`psdomain://concepts` resource are registered, and the helpers all tool groups share:
caller-identity resolution from the verified bearer token, role gating, graph-opener sanitising
and the started/succeeded/failed log triad wrapped around every tool body. Each tool group
lives in its own `*_tools` module and registers on `server`; the front door lists them all in
`mcp_server.TOOL_GROUP_MODULES`.

Patch seam:
    The store classes `PsycopgAccessRoleStore`, `PsycopgAuditStore` and
    `PsycopgPendingApprovalStore` and the loader `connect_from_config` are bound in this module
    and nowhere else in the package. Every consumer, in this module and in every tool-group module,
    looks them up here when it calls them (`tool_core.PsycopgAuditStore(config)`), never through an
    import of its own. They are the single seam tests and deployments patch: one
    `monkeypatch.setattr(tool_core, "<Name>", ...)` reaches every tool of every group.

Business Rules:
    - This module binds no transport and no authentication. The Streamable HTTP transport in the
      sibling `http_transport` module is the only transport and arms authentication on `server` from
      outside this file, so importing this module performs no I/O and no fail-closed check.
    - A tool failure crosses the MCP boundary as an `error: <message>` string. Infrastructure
      failures use fixed, detail-free wording: no host, port, driver or credential text.
"""

from __future__ import annotations

import dataclasses
import os
from typing import TYPE_CHECKING, Final

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import (
    Context,  # noqa: TC002 -- the MCP SDK's own Context-injection resolves this annotation at runtime
)

from ps_service.api.ingestion_orchestration import (
    _STAGE_REASON_MAX_LEN,  # pyright: ignore[reportPrivateUsage]  -- shared failure-reason cap, reused for `_run_mcp_action`'s own truncation; mirrors change_check_orchestration.py's cross-module private-import convention
)
from ps_service.api.stage_failure_log import stage_failure_detail
from ps_service.audit.actor import resolve_audit_actor
from ps_service.audit.emit import AuditContext
from ps_service.audit.errors import (
    AuditActorUnresolvedError,
)
from ps_service.audit.store import PsycopgAuditStore
from ps_service.authz.models import AccessRole
from ps_service.authz.store import PsycopgAccessRoleStore
from ps_service.config import (
    LOCAL_TEST_PRINCIPAL_ID,
    load_config,
)
from ps_service.logging import (
    bind_run_context,
    emit_log_entry,
)
from ps_service.mcp_interface.errors import (
    McpGraphUnavailableError,
)
from ps_service.mcp_interface.tool_gates import (
    BypassPolicy,
    GateEnvironment,
    GateKind,
    ToolGate,
    gated_tool,
)
from ps_service.passkey_signing.store import PsycopgPendingApprovalStore
from ps_service.passkey_signing.webauthn_rp import signable_link_host
from ps_service.query_engine.falkordb_client import (
    GraphHandle,
    connect_from_config,
    select_graph,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.config import ServiceConfig
    from ps_service.graph_cleanup.dependencies import (
        GraphCleanupDependencies,
    )

__all__ = [
    "ACCESS_ROLE_CHANGE_GATE",
    "ACCESS_ROLE_MANAGEMENT_GATE",
    "ACCESS_ROLE_MANAGEMENT_REQUIRES_AUTHENTICATED_CALLER_MESSAGE",
    "CELEX_PATTERN",
    "CLEANUP_APPROVAL_UNSETTLED_MESSAGE",
    "CLEANUP_GATE",
    "COMPLIANCE_OFFICER_GATE",
    "COMPONENT",
    "GRAPH_UNAVAILABLE_MESSAGE",
    "MCP_CALLER_LABEL",
    "NEAR_MISS_MERGE_GATE",
    "OWN_APPROVAL_READ_GATE",
    "PLAIN_READ_GATE",
    "POLICY_AUTHOR_GATE",
    "POLICY_DRAFT_ACCESS_GATE",
    "SYSTEM_ADMIN_GATE",
    "UNEXPECTED_ERROR_MESSAGE",
    "PsycopgAccessRoleStore",
    "PsycopgAuditStore",
    "PsycopgPendingApprovalStore",
    "audit_context",
    "connect_from_config",
    "gate_environment",
    "gated_actor",
    "gated_server_tool",
    "resolve_authz_actor",
    "resolve_base_url",
    "resolve_graph",
    "resolve_policy_lifecycle_actor",
    "run_mcp_action",
    "sanitize_cleanup_graph_open",
    "sanitize_graph_open",
    "server",
]

COMPONENT = "mcp_interface"
# The `caller`/`actor` value MCP passes to ingestion and restore logging; not a user identifier.
MCP_CALLER_LABEL: Final = "mcp"
_DEFAULT_GRAPH_NAME = "policy_system"
_GRAPH_UNAVAILABLE_DETAIL = "the policy graph database is not reachable"
GRAPH_UNAVAILABLE_MESSAGE = f"error: {_GRAPH_UNAVAILABLE_DETAIL}"
UNEXPECTED_ERROR_MESSAGE = "error: an unexpected error occurred"
# Same CELEX pattern `api/models.py`'s `CatalogIngestionRequest.celex` already enforces.
CELEX_PATTERN = r"^3\d{4}[A-Z]\d{4}$"


def _graph_name() -> str:
    """Return the company graph name used by every graph-backed tool.

    Reads `PS_FALKORDB_GRAPH` directly, defaulting to `policy_system`, because the service
    configuration deliberately has no field for it.

    Returns:
        The graph name.

    Raises:
        McpGraphUnavailableError: The variable is set but blank.
    """
    name = os.environ.get("PS_FALKORDB_GRAPH", _DEFAULT_GRAPH_NAME)
    if not name.strip():
        raise McpGraphUnavailableError(_GRAPH_UNAVAILABLE_DETAIL)
    return name


server = MCPServer(
    name="ps-mcp",
    instructions=(
        "Read-only Cypher access to the policy_system compliance graph. "
        "Call the domain_concepts tool first and ground every query in its actual node "
        "labels, properties, and edge directions -- never invent one. "
        "Write clauses are rejected before execution and returned as an 'error:' line."
    ),
)


gate_environment = GateEnvironment(
    load_config=lambda: load_config(),  # noqa: PLW0108
    resolve_actor=lambda config: resolve_authz_actor(config),  # noqa: PLW0108
    role_store_factory=lambda config: PsycopgAccessRoleStore(
        config, audit_store=PsycopgAuditStore(config)
    ),
    audit_store_factory=lambda config: PsycopgAuditStore(config),  # noqa: PLW0108
    run_action=lambda action, body: run_mcp_action(action, body),  # noqa: PLW0108
)


gated_server_tool = gated_tool(server, gate_environment)


def resolve_graph(config: ServiceConfig) -> GraphHandle:
    """Open the company graph for one tool call, hiding connection detail from the client.

    Args:
        config: Already-resolved service configuration. The caller resolves it, so a
            `ServiceConfigurationError` raised while loading it is sanitised by the caller.

    Returns:
        A `GraphHandle` for the graph named by `_graph_name()`.

    Raises:
        McpGraphUnavailableError: The graph name is blank, or the database is unreachable or refused
            the connection, or the driver failed while connecting or selecting the graph. The
            message is fixed and generic, because host, port, driver and environment-variable text
            must not cross the MCP boundary; the original cause is chained.

    Side Effects:
        Opens a connection to FalkorDB.
    """
    try:
        return select_graph(connect_from_config(config), _graph_name())
    except McpGraphUnavailableError:
        raise
    # broad by design: every failure here is sanitised to a fixed message
    # and chained, never re-raised raw
    except Exception as exc:
        raise McpGraphUnavailableError(_GRAPH_UNAVAILABLE_DETAIL) from exc


ACCESS_ROLE_MANAGEMENT_REQUIRES_AUTHENTICATED_CALLER_MESSAGE = (
    "error: access-role management requires a real authenticated caller"
)


# Defensive fallback for a non-bypass call with no verified bearer token bound to it. It is
# unreachable in practice (`ps_service.main.create_app` fails closed at startup whenever no real
# authentication is configured and the bypass is off), but it keeps the gate fail-closed rather
# than passing `None` through to the role gate should that startup invariant ever be violated.
_CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE = (
    "error: this action requires a real authenticated caller"
)


def resolve_authz_actor(config: ServiceConfig) -> tuple[str, str] | None:
    """Resolve the `(sub, iss)` identity used for role checks, role management and audit actors.

    Same extraction as `_resolve_signing_actor`, kept as its own function on purpose: signing
    identity and access-role identity are owned independently, and one shared helper would let a
    change to either's rules silently change the other's. There is deliberately no fallback to
    `LOCAL_TEST_PRINCIPAL_ID`: access-role tools read and write real, permanent, identity-keyed
    rows, and a synthetic bypass identity must never hold elevated access in a real store.

    Args:
        config: Accepted so every resolver shares one signature; unused.

    Returns:
        The verified `(sub, iss)` pair, or `None` if no verified bearer token is bound to this call
        (the bypass included), the token has no subject, or its `iss` claim is not a string.
    """
    del config
    access_token = get_access_token()
    if access_token is None or access_token.subject is None:
        return None
    iss = (access_token.claims or {}).get("iss")
    if not isinstance(iss, str):
        return None
    return access_token.subject, iss


def gated_actor(config: ServiceConfig) -> tuple[str, str]:
    """Return the verified `(sub, iss)` of a caller the tool's role gate has already admitted.

    Only for a tool declared as a `ROLE` gate that refuses the local-test bypass: the gate wrapper
    has already turned a missing identity into the unauthenticated message, so none is missing here.

    Args:
        config: Passed to `resolve_authz_actor`.

    Returns:
        The verified `(sub, iss)` pair.
    """
    actor = resolve_authz_actor(config)
    assert actor is not None  # noqa: S101  # the gate wrapper has already refused a caller with no identity
    return actor


def resolve_policy_lifecycle_actor(config: ServiceConfig) -> tuple[str, str] | None:
    """Resolve the `(sub, iss)` identity for the policy-lifecycle tools.

    Same verified-bearer-token branch as `resolve_authz_actor`, but it falls back to
    `(LOCAL_TEST_PRINCIPAL_ID, LOCAL_TEST_PRINCIPAL_ID)` when no bearer token is bound and the
    local-test bypass is active. A Policy draft created under the bypass is what local,
    unauthenticated end-to-end testing needs, and it corrupts no real identity-keyed record. The
    fallback applies only when no token is present at all: a present but malformed token (no
    subject, or an `iss` that is not a string) resolves to `None` and never falls through to the
    bypass identity.

    Args:
        config: Service configuration; only `is_local_test_bypass_active` is read.

    Returns:
        The verified `(sub, iss)` pair; the local-test pair when no token is bound and the bypass is
        active; otherwise `None`.
    """
    access_token = get_access_token()
    if access_token is not None:
        if access_token.subject is None:
            return None
        iss = (access_token.claims or {}).get("iss")
        if not isinstance(iss, str):
            return None
        return access_token.subject, iss
    if config.is_local_test_bypass_active:
        return LOCAL_TEST_PRINCIPAL_ID, LOCAL_TEST_PRINCIPAL_ID
    return None


def audit_context(config: ServiceConfig, actor: tuple[str, str] | None) -> AuditContext | str:
    """Build the audit context (actor plus audit store) for one audited tool call.

    The single MCP definition of the actor policy, the counterpart of REST's
    `provide_audit_context`. The actor is the verified `(sub, iss)`, or the local-test-bypass
    sentinel when there is none and the bypass is active. `PsycopgAuditStore` opens no connection
    until a row is written, so a tool that needs only `.actor` pays nothing for the store.

    Args:
        config: Service configuration; selects the audit database and the bypass state.
        actor: Verified `(sub, iss)` from one of the resolvers, or `None` when none was bound.

    Returns:
        An `AuditContext`, or `_CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE` (fail-closed,
        returned by the tool as is) when there is no actor and the bypass is inactive.
    """
    try:
        return AuditContext(
            actor=resolve_audit_actor(
                actor, is_local_test_bypass_active=config.is_local_test_bypass_active
            ),
            store=PsycopgAuditStore(config),
        )
    except AuditActorUnresolvedError:
        return _CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE


def resolve_base_url(ctx: Context) -> str:
    """Derive the `{scheme}://{host}` base URL of the live MCP request, for approval links.

    Scheme and host come off the live request rather than being hardcoded, so an approval link is
    correct under a NodePort, a ClusterIP with Ingress and local development alike. It reads
    `ctx.request_context.request` (the Streamable HTTP transport's raw Starlette `Request`) because
    `Context` does not expose the scheme. `signable_link_host` rewrites a loopback IP literal to
    `localhost`; without that a link from a local bind could not be signed. `api.routes` applies the
    same substitution, so both transports hand out equally signable links.

    Args:
        ctx: MCP request context of the current tool call.

    Returns:
        `scheme://host`. The scheme defaults to `http` when the request has none, and
        `scheme://unknown` is returned when no request is bound or it has no host header (only a
        bare, context-less test invocation reaches that case).
    """
    try:
        request = ctx.request_context.request
    except ValueError:
        request = None
    scheme = getattr(getattr(request, "url", None), "scheme", None) or "http"
    headers = getattr(request, "headers", None)
    host = headers.get("host", "") if headers is not None else ""
    return f"{scheme}://{signable_link_host(host)}" if host else f"{scheme}://unknown"


_CLEANUP_REQUIRES_AUTHENTICATED_CALLER_MESSAGE = (
    "error: graph cleanup requires a real authenticated caller"
)


SYSTEM_ADMIN_GATE = ToolGate(
    kind=GateKind.ROLE,
    role=AccessRole.SYSTEM_ADMIN,
    bypass=BypassPolicy.SKIP,
    unauthenticated_message=_CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE,
)


ACCESS_ROLE_MANAGEMENT_GATE = ToolGate(
    kind=GateKind.ROLE,
    role=AccessRole.SYSTEM_ADMIN,
    bypass=BypassPolicy.REFUSE,
    unauthenticated_message=ACCESS_ROLE_MANAGEMENT_REQUIRES_AUTHENTICATED_CALLER_MESSAGE,
)


CLEANUP_GATE = ToolGate(
    kind=GateKind.ROLE,
    role=AccessRole.COMPLIANCE_OFFICER,
    bypass=BypassPolicy.REFUSE,
    unauthenticated_message=_CLEANUP_REQUIRES_AUTHENTICATED_CALLER_MESSAGE,
)


NEAR_MISS_MERGE_GATE = ToolGate(
    kind=GateKind.BODY,
    role=AccessRole.COMPLIANCE_OFFICER,
    bypass=BypassPolicy.REFUSE,
    unauthenticated_message=_CLEANUP_REQUIRES_AUTHENTICATED_CALLER_MESSAGE,
)


COMPLIANCE_OFFICER_GATE = ToolGate(
    kind=GateKind.ROLE,
    role=AccessRole.COMPLIANCE_OFFICER,
    bypass=BypassPolicy.SKIP,
    unauthenticated_message=_CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE,
)


PLAIN_READ_GATE = ToolGate(
    kind=GateKind.AUTHENTICATED_ONLY,
    reason="a plain read of the graph or the curated catalog; any authenticated caller",
)


OWN_APPROVAL_READ_GATE = ToolGate(
    kind=GateKind.AUTHENTICATED_ONLY,
    reason="the tool reads only an approval the caller created; it needs a real session",
)


ACCESS_ROLE_CHANGE_GATE = ToolGate(
    kind=GateKind.SELF_AUDITED,
    reason=(
        "the per-role actor requirement is enforced in grant_role/revoke_role, which write the "
        "access_role rejected row themselves"
    ),
)


POLICY_AUTHOR_GATE = ToolGate(
    kind=GateKind.AUTHENTICATED_ONLY,
    reason="any authenticated caller may create a draft or act on a Policy they own",
)


POLICY_DRAFT_ACCESS_GATE = ToolGate(
    kind=GateKind.AUTHENTICATED_ONLY,
    reason="the Policy owner, SystemOwner or SystemAdmin; enforced in the lifecycle service",
)


def run_mcp_action(
    action: str,
    body: Callable[[], dict[str, object] | str],
    *,
    entity_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, object] | str:
    """Run one MCP tool body in a run context with a uniform started/succeeded/failed log triad.

    The shared helper for every action-taking tool, so the log triad and the generic-exception
    safety net live in exactly one place. It binds a fresh run id, emits a
    `component="mcp_interface"` `outcome="started"` entry, then runs `body`; the started and
    terminal entries are correlated by that one `run_id` and record no caller identity (the
    caller is attributed in the audit trail, not the log). A `body` that returns a string
    beginning `"error:"` is a handled failure: it is logged `outcome="failed"` with the
    (truncated) text as `reason` and returned unchanged. A `body` that raises `Exception` is the
    residual, not-yet-sanitised case: it is converted to the fixed generic error string, with the
    full `repr` logged server-side only. Any other return value is a success, logged
    `outcome="succeeded"` and returned unchanged.

    Args:
        action: The tool's own name (for example `"ingest_regulation"`), used as the log entries'
            `action` field.
        body: Zero-argument callable doing the tool's work inside the bound run context; it reads
            its run id through `current_run_id()` because binding happens here, not in `body`.
        entity_id: Id of the node this call acts on (for a not-yet-created node, its parent),
            carried on all three log entries. `None` leaves `LogEntry.entity_id` unset.
        run_id: Run id to bind instead of a freshly generated uuid4, so a background worker's log
            entries share the id of the submitting call. `None` generates a uuid4.

    Returns:
        `body`'s return value unchanged on success or a handled `error:` string, or the fixed
        generic error string if `body` raised.

    Raises:
        LoggingLifecycleError: No log emitter is available when one of the triad entries is emitted.
        BaseException: Subclasses that are not `Exception` (such as `KeyboardInterrupt`) propagate
            from `body`.

    Side Effects:
        Binds a run context for the call and emits two log entries (started, then succeeded or
        failed). Running `body` has whatever effects the tool defines.
    """
    with bind_run_context(run_id) as bound_run_id:
        emit_log_entry(
            component=COMPONENT,
            action=action,
            outcome="started",
            run_id=bound_run_id,
            entity_id=entity_id,
        )
        try:
            result = body()
        except Exception as exc:  # noqa: BLE001 -- residual MCP-boundary safety net: body() must never raise across the MCP boundary
            emit_log_entry(
                component=COMPONENT,
                action=action,
                outcome="failed",
                run_id=bound_run_id,
                entity_id=entity_id,
                extra={
                    "exception_type": type(exc).__name__,
                    "detail": stage_failure_detail(exc),
                },
            )
            return UNEXPECTED_ERROR_MESSAGE
        if isinstance(result, str) and result.startswith("error:"):
            emit_log_entry(
                component=COMPONENT,
                action=action,
                outcome="failed",
                run_id=bound_run_id,
                entity_id=entity_id,
                extra={"reason": result[:_STAGE_REASON_MAX_LEN]},
            )
            return result
        emit_log_entry(
            component=COMPONENT,
            action=action,
            outcome="succeeded",
            run_id=bound_run_id,
            entity_id=entity_id,
        )
        return result


def sanitize_graph_open[**P, R](opener: Callable[P, R]) -> Callable[P, R]:
    """Wrap one graph opener so any failure becomes `McpGraphUnavailableError`.

    Keeps host, port and driver detail from crossing the MCP boundary, as `resolve_graph` does for
    the `cypher` tool.

    Args:
        opener: Callable that opens a graph or database connection.

    Returns:
        A callable with `opener`'s signature that returns `opener`'s result.

    Raises:
        McpGraphUnavailableError: Raised by the returned callable, with fixed generic detail and the
            original error chained, when `opener` raises any `Exception`.
    """

    def _wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return opener(*args, **kwargs)
        except Exception as exc:
            raise McpGraphUnavailableError(_GRAPH_UNAVAILABLE_DETAIL) from exc

    return _wrapped


def sanitize_cleanup_graph_open(
    dependencies: GraphCleanupDependencies,
) -> GraphCleanupDependencies:
    """Return `dependencies` with its graph opener wrapped by `sanitize_graph_open`."""
    return dataclasses.replace(
        dependencies,
        open_single_tenant_graph=sanitize_graph_open(dependencies.open_single_tenant_graph),
    )


CLEANUP_APPROVAL_UNSETTLED_MESSAGE = (
    "error: the approval status could not be settled right now; try again shortly"
)
