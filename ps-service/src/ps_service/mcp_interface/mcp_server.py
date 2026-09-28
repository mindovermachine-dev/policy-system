#!/usr/bin/env python3
"""MCP server definition: in-process read-only Cypher access to the policy_system graph.

Defines the `cypher` and `domain_concepts` tools and the
`psdomain://concepts` resource on a single `MCPServer` instance. Calls
ps_service.query_engine.execute_cypher_query IN-PROCESS; the write-clause
guard and all execution live in Query Engine and are never duplicated here.

This module defines the server surface only -- it binds no transport and
no auth of its own. The Streamable HTTP transport (issue #39) is the sole
transport and lives in the sibling `http_transport.py` module, which calls
`server.streamable_http_app(...)` on the `server` object defined below
from outside this file -- see `tests/mcp_interface/test_scope_guard.py`
for the guards this keeps intact. MCP's stdio transport was removed once
the plugin model made the HTTP endpoint the only supported client path.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import os
from datetime import (
    datetime,  # noqa: TC003 -- `list-audit-events`'s own tool params carry this type at runtime; the MCP SDK's `func_metadata` resolves `from __future__ import annotations`-deferred string annotations via `get_type_hints`, which needs `datetime` in this module's real globals, not TYPE_CHECKING-only
)
from importlib import resources
from typing import TYPE_CHECKING, Annotated, Literal

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import (
    Context,  # noqa: TC002 -- the MCP SDK's own Context-injection resolves this annotation at runtime
)
from pydantic import AfterValidator, Field

from ps_service import dependency_health
from ps_service.api.catalog import find_by_celex
from ps_service.api.change_check_orchestration import (
    build_default_change_check_dependencies,
    run_change_check_sweep,
)
from ps_service.api.errors import (
    AccessDeniedError,
    AuthorizationStoreUnavailableError,
    CatalogIdentifierNotFoundError,
    CuratedSourceUnavailableError,
    IngestionConfigIncompleteError,
    InvalidAccessRoleError,
    InvalidAuditQueryFilterError,
    PendingReviewNotFoundError,
    PipelineStageError,
    RestoreArtifactRejectedError,
    RestoreStageFailedError,
    SelfGrantOrRevokeBlockedError,
    SystemOwnerFloorViolationError,
)
from ps_service.api.ingestion_orchestration import (
    _STAGE_REASON_MAX_LEN,  # pyright: ignore[reportPrivateUsage]  -- shared failure-reason cap; D-AUDIT-WRAPPER reuses it for `_run_mcp_action`'s own truncation, mirrors change_check_orchestration.py's own cross-module private-import convention
    GraphOpeners,
    build_default_pipeline_dependencies,
    resolve_via_cellar,
    run_catalog_ingestion_pipeline,
)
from ps_service.api.models import (
    CatalogInstrumentEntry,
    CatalogRestorationRequest,
    CuratedCatalogResponse,
)
from ps_service.api.near_miss_review_orchestration import (
    build_default_near_miss_review_dependencies,
    run_list_near_misses,
    run_resolve_near_miss,
)
from ps_service.api.restore_orchestration import (
    build_default_restore_from_catalog_dependencies,
    run_restoration_from_catalog_source,
)
from ps_service.api.routes import (
    _to_accepted_response,  # pyright: ignore[reportPrivateUsage]  -- D-RESPONSE-SHAPE: reuse the REST wire-shaping helper verbatim so the MCP and REST paths can never silently drift, mirrors change_check_orchestration.py's own cross-module private-import convention
    _to_change_check_response,  # pyright: ignore[reportPrivateUsage]  -- D-RESPONSE-SHAPE: same reuse for `check_regulations`, mirrors `_to_accepted_response`'s own precedent immediately above
)
from ps_service.audit.models import AuditQueryFilters
from ps_service.audit.store import PsycopgAuditStore
from ps_service.authz.models import AccessRole
from ps_service.authz.service import (
    grant_role,
    list_assignments,
    require_role,
    revoke_role,
)
from ps_service.authz.service import (
    list_audit_events as run_list_audit_events,
)
from ps_service.authz.store import PsycopgAccessRoleStore
from ps_service.config import LOCAL_TEST_PRINCIPAL_ID, ServiceConfigurationError, load_config
from ps_service.curated_source import store as catalog_source_store
from ps_service.curated_source.catalog_client import build_default_curated_catalog_dependencies
from ps_service.curated_source.errors import (
    CuratedSourceConfigurationError,
    CuratedSourceFetchError,
)
from ps_service.curated_source.resolve import resolve_effective_source
from ps_service.curated_source.source_url import validate_source_url
from ps_service.invitations.client import create_invitation
from ps_service.invitations.errors import AuthentikInvitationError
from ps_service.logging import (
    LoggingLifecycleError,
    bind_run_context,
    current_run_id,
    emit_log_entry,
)
from ps_service.mcp_interface.errors import (
    McpGraphUnavailableError,
    McpResourceUnavailableError,
)
from ps_service.passkey_signing.service import check_pending_approval, create_merge_pending_approval
from ps_service.passkey_signing.store import PsycopgPendingApprovalStore
from ps_service.query_engine import (
    GraphUnseededError,
    QueryEngineExecutionError,
    QueryResult,
    WriteClauseRejectedError,
    execute_cypher_query,
)
from ps_service.query_engine.falkordb_client import (
    GraphHandle,
    connect_from_config,
    select_graph,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from importlib.resources.abc import Traversable

    from ps_service.api.catalog import CatalogEntry
    from ps_service.api.change_check_orchestration import ChangeCheckDependencies
    from ps_service.api.ingestion_orchestration import PipelineDependencies
    from ps_service.api.near_miss_review_orchestration import NearMissReviewDependencies
    from ps_service.api.restore_orchestration import CatalogRestoreDependencies
    from ps_service.audit.models import AuditEventRow
    from ps_service.authz.models import AccessRoleAssignmentRow
    from ps_service.config import ServiceConfig
    from ps_service.curated_source.catalog_client import CuratedCatalogDependencies
    from ps_service.ingestion.adapters.base import IngestionAdapter
    from ps_service.logging.emitter import LogEmitter

_COMPONENT = "mcp_interface"
_ACTION = "handle_mcp_tool_call"
_DEFAULT_GRAPH_NAME = "policy_system"
_DOMAIN_CONCEPTS_URI = "psdomain://concepts"
_DOMAIN_CONCEPTS_UNAVAILABLE_DETAIL = "the ps-domain-concepts resource is currently unavailable"
_GRAPH_UNAVAILABLE_DETAIL = "the policy graph database is not reachable"
_GRAPH_UNAVAILABLE_MESSAGE = f"error: {_GRAPH_UNAVAILABLE_DETAIL}"
_UNEXPECTED_ERROR_MESSAGE = "error: an unexpected error occurred"
# D-PREFLIGHT: verbatim reuse of ps-cli's own `handlers.py:72` message text,
# preserved for operator familiarity across both client paths.
_LLM_INTERFACE_UNAVAILABLE_MESSAGE = "error: LLM Interface is unavailable."

# D-SHORTNAME-PATTERN: must start with a letter, alnum/`_`/`-` body, 1-64 chars --
# mirrors every existing catalog short_name ("cra", "gdpr", "nis2").
_SHORT_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"
# Same CELEX pattern `api/models.py`'s `CatalogIngestionRequest.celex` already enforces.
_CELEX_PATTERN = r"^3\d{4}[A-Z]\d{4}$"
# D-INSTRUMENT-ID-STRICTNESS: same charset/length bound as ps-cli's own
# `_INSTRUMENT_ID_PATTERN` (`ps-cli/src/ps_cli/modules/parser.py:31`) -- at least as
# strict as `CatalogRestorationRequest.instrument_id`'s looser REST-sibling pattern
# (`api/models.py:181-183`, no `".."` guard of its own). PLAN.md's own proposed
# single-regex collapse (`^(?!.*\.\.)...`) does not work here: pydantic-core's regex
# backend (the Rust `regex` crate, wired in by the MCP SDK's own `func_metadata` ->
# `create_model` call) does not support look-around at all -- confirmed by the
# `SchemaError: look-around... is not supported` raised at tool-registration time
# when that pattern was tried. `_reject_path_traversal_segment` below (an
# `AfterValidator`) supplies the `".."` rejection instead, still entirely within
# pydantic's own schema-validation pass -- i.e. still before this tool's body ever
# runs -- mirroring ps-cli's own two-step `_instrument_id_type` check (regex
# fullmatch, then an explicit `".." in value` scan) rather than collapsing it.
_RESTORE_INSTRUMENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
# A deliberately permissive structural "looks like an email" check for
# `invite_user` (issue #140, AC-BI-007) -- full RFC 5322 validation is out of
# scope; `pydantic.EmailStr` needs the `email-validator` package, which is
# not an installed dependency in this workspace, and adding it is unwarranted
# scope creep for a trust-boundary check whose real validation authority is
# Authentik itself at redemption time.
_INVITE_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def _reject_path_traversal_segment(value: str) -> str:
    """`AfterValidator` companion to `_RESTORE_INSTRUMENT_ID_PATTERN` (D-INSTRUMENT-ID-STRICTNESS).

    Runs immediately after the base charset pattern matches, within the same
    pydantic schema-validation pass the MCP SDK runs before dispatching to
    `restore_instrument`'s body -- see `_RESTORE_INSTRUMENT_ID_PATTERN`'s own
    comment for why this is a separate validator rather than a single regex.
    """
    if ".." in value:
        msg = "instrument_id must not contain '..'"
        raise ValueError(msg)
    return value


@functools.cache
def _domain_concepts_path() -> Traversable:
    """Packaged location of ps-domain-concepts.md (issue #39, AC-BI-012).

    Resolved via `importlib.resources` against this installed package, not
    a repo-checkout-relative path -- so the resource serves correctly from
    a wheel install with no repo checkout present, not only from an
    editable/dev install. Lazy + cached: never touched at import.
    """
    return resources.files("ps_service.mcp_interface").joinpath("ps-domain-concepts.md")


def _graph_name() -> str:
    """The single company-graph name.

    Reads PS_FALKORDB_GRAPH directly (config.py deliberately has no
    falkordb_graph field -- see PLAN_REVIEWED §2 Q4). Rejects an
    explicitly-empty value, mirroring config._parse_falkordb_host.
    """
    name = os.environ.get("PS_FALKORDB_GRAPH", _DEFAULT_GRAPH_NAME)
    if not name.strip():
        raise McpGraphUnavailableError(_GRAPH_UNAVAILABLE_DETAIL)
    return name


server = MCPServer(
    name="policy-system-graph",
    instructions=(
        "Read-only Cypher access to the policy_system compliance graph. "
        "Call the domain_concepts tool first (the same text as the psdomain://concepts "
        "resource) and ground every query in its actual node labels, properties, and "
        "edge directions -- never invent one. "
        "Write clauses are rejected before execution and returned as an 'error:' line."
    ),
)


def handle_mcp_tool_call(
    query: str,
    *,
    graph: GraphHandle,
    emitter: LogEmitter | None = None,
    principal: str | None = None,
    timeout_ms: int,
    row_cap: int,
) -> dict[str, object] | str:
    """HandleMcpToolCall: run `query` through Query Engine in-process.

    Binds a fresh run_id, then returns `{columns, rows, row_count, truncated}`
    on success or an `error: <message>` string verbatim on a rejected,
    unseeded-graph, or failed query.

    `principal` is an opaque caller identity string (issue #67), threaded
    straight through to `execute_cypher_query` so it lands on the
    `query_engine` log entry; omitted entirely when `None` (the default),
    matching Slice 3's silent-by-default behavior end to end. This layer
    never decides who the principal is -- see Slice 5 for where it's set.

    `timeout_ms`/`row_cap` (issue #38, D4) are required and threaded
    straight through to `execute_cypher_query` unchanged -- this layer never
    decides the bounds, only threads what its own caller (`cypher()`, wired
    from `ServiceConfig`) gives it.
    """
    with bind_run_context():
        try:
            result: QueryResult = execute_cypher_query(
                query,
                graph=graph,
                emitter=emitter,
                principal=principal,
                timeout_ms=timeout_ms,
                row_cap=row_cap,
            )
        except (WriteClauseRejectedError, QueryEngineExecutionError, GraphUnseededError) as exc:
            return f"error: {exc}"
    return {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "truncated": result.truncated,
    }


def _resolve_graph(config: ServiceConfig) -> GraphHandle:
    """Acquire a GraphHandle for one tool call, given an already-resolved config.

    ANY failure here -- DB unreachable/refused, driver I/O in the eager
    FalkorDB constructor or in select_graph -- is sanitised to a fixed
    generic McpGraphUnavailableError. Host, port, driver, and env-var text
    must not cross the MCP boundary (L2 MCP Interface Patterns; PLAN_REVIEWED
    §2 Q6 / F-01 / F-17). `config` is resolved by the caller (`cypher()`), not
    here, so a `ServiceConfigurationError` from `load_config()` is sanitised
    by the caller's own try/except rather than this function's.
    """
    try:
        return select_graph(connect_from_config(config), _graph_name())
    except McpGraphUnavailableError:
        raise
    # broad by design: every failure here is sanitised to a fixed message
    # and chained, never re-raised raw
    except Exception as exc:
        raise McpGraphUnavailableError(_GRAPH_UNAVAILABLE_DETAIL) from exc


def _resolve_principal(config: ServiceConfig) -> str | None:
    """Resolve the `cypher` tool's caller identity (issue #58, AC-BI-007; issue #67).

    A verified bearer token's `sub` always wins when one is present --
    `get_access_token()` reads the `AccessToken` the MCP SDK's own
    `AuthContextMiddleware` stashed on a contextvar for this request/task,
    installed automatically by the SDK whenever `token_verifier` is set
    (`ps_service.mcp_interface.http_transport.build_streamable_http_app`).
    Falls back to the fixed local-test principal only when no token was
    verified for this call AND the bypass (#67) is active -- the bypass's
    existing, unchanged contract. Otherwise `None`, exactly as before this
    issue (no real auth configured and no bypass: unreachable in practice,
    since `ps_service.main.create_app` fails closed at startup in that
    case, but this function makes no such assumption itself).
    """
    access_token = get_access_token()
    if access_token is not None:
        return access_token.subject
    if config.is_local_test_bypass_active:
        return LOCAL_TEST_PRINCIPAL_ID
    return None


def _resolve_signing_actor() -> tuple[str, str] | None:
    """Resolve `(sub, iss)` for the merge-gating flow's caller identity (issue #131, PLAN.md §2.1).

    Narrower than `_resolve_principal`: a signed passkey approval must be
    bound to both `sub` and `iss` (AC-BI-004/AC-BI-005), not `sub` alone --
    mirrors `RestAuthMiddleware.__call__`'s own identical `sub`/`iss`
    extraction from the same `AccessToken` shape (`auth/middleware.py`),
    not a new pattern.

    Deliberately does **not** fall back to `LOCAL_TEST_PRINCIPAL_ID` the way
    `_resolve_principal` does: under the local-test bypass there is no real
    actor identity a WebAuthn credential could ever meaningfully belong to,
    so a pending approval must never be created for it (AC-BI-002 requires
    "an already-authenticated caller under the existing OIDC session
    contract" -- the bypass has no such contract). Returns `None` in that
    case, which each caller (this module's `near_misses_resolve`/
    `near_misses_check_approval`) treats as a fail-closed condition.

    Returns:
        The verified `(sub, iss)` pair, or `None` if no verified bearer
        token is bound to this call.
    """
    access_token = get_access_token()
    if access_token is None or access_token.subject is None:
        return None
    iss = (access_token.claims or {}).get("iss")
    if not isinstance(iss, str):
        return None
    return access_token.subject, iss


_ACCESS_ROLE_MANAGEMENT_REQUIRES_AUTHENTICATED_CALLER_MESSAGE = (
    "error: access-role management requires a real authenticated caller"
)

# PLAN.md §3.3/§4 Slice 4: the catalog-source gate's own defensive fallback for
# a non-bypass call with no verified bearer token bound to it -- unreachable
# in practice (`ps_service.main.create_app` fails closed at startup whenever
# no real auth is configured and the bypass is off, mirroring
# `_resolve_principal`'s own documented invariant), but this keeps the gate
# fail-closed rather than passing `None` through to `require_role` should
# that invariant ever be violated.
_CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE = (
    "error: this action requires a real authenticated caller"
)


def _resolve_authz_actor(config: ServiceConfig) -> tuple[str, str] | None:
    """Resolve `(sub, iss)` for the access-role management flow's caller identity (issue #133).

    Structurally identical to `_resolve_signing_actor` above but kept as its
    own function -- a deliberate small duplication (L1 "prefer duplication
    over the wrong abstraction", PLAN.md §3.1): the two concepts
    (signing-ceremony ownership identity vs. access-role identity) are
    independently owned, and coupling them through one shared private helper
    would mean a future change to either component's own identity-resolution
    rules silently changes the other's behavior too.

    Deliberately does **not** fall back to `LOCAL_TEST_PRINCIPAL_ID` the way
    `_resolve_principal` does (PLAN.md §3.3): `grant-access-role`/
    `revoke-access-role`/`list-access-roles` write/read real, permanent,
    identity-keyed Postgres rows, and bootstrapping the fixed bypass
    principal string as `SystemOwner` in a real store would be actively
    wrong -- a meaningless synthetic identity holding real elevated access.
    `config` is accepted (unused beyond documenting this contract) so every
    caller passes it uniformly, matching `_resolve_principal`'s own
    signature shape.

    Returns:
        The verified `(sub, iss)` pair, or `None` if no verified bearer
        token is bound to this call (bypass included).
    """
    del config
    access_token = get_access_token()
    if access_token is None or access_token.subject is None:
        return None
    iss = (access_token.claims or {}).get("iss")
    if not isinstance(iss, str):
        return None
    return access_token.subject, iss


def _resolve_base_url(ctx: Context) -> str:
    """Derive `{scheme}://{host}` from the live MCP request (PLAN.md §2.2 step 4).

    Mirrors `ps_service.auth.middleware._resource_metadata_url`'s own
    "scheme + host off the live request, never hardcoded" pattern, so the
    approval link is correct under a `kind` NodePort, a ClusterIP+Ingress,
    and local dev alike. Reads `ctx.request_context.request` directly (the
    Streamable HTTP transport's own raw Starlette `Request`, confirmed
    against `mcp.server._streamable_http_modern`) rather than `ctx.headers`
    alone, since `Context` does not expose scheme separately. Falls back to
    a fixed placeholder when no request is bound to this call at all --
    never reachable via the real Streamable HTTP transport; only a bare,
    context-less test-only tool invocation hits this branch.
    """
    try:
        request = ctx.request_context.request
    except ValueError:
        request = None
    scheme = getattr(getattr(request, "url", None), "scheme", None) or "http"
    headers = getattr(request, "headers", None)
    host = headers.get("host", "") if headers is not None else ""
    return f"{scheme}://{host}" if host else f"{scheme}://unknown"


def _run_mcp_action(
    action: str,
    principal: str | None,
    body: Callable[[], dict[str, object] | str],
) -> dict[str, object] | str:
    """Run one MCP tool body inside a fresh run context, with a uniform audit triad.

    D-AUDIT-WRAPPER: the shared logging/run-id helper every action-taking MCP
    tool (`ingest_regulation`, `check_regulations`, `near_misses_list`,
    `near_misses_resolve`) reuses, so the started/succeeded/failed triad and
    generic-exception safety net live in exactly one place rather than being
    duplicated a fourth time (L2 Common DRY).

    Binds a fresh run id for the call, emits a `component="mcp_interface"`
    `outcome="started"` entry carrying `principal`, then runs `body`. A
    `body` that returns a string beginning `"error:"` is treated as a
    handled failure -- logged `outcome="failed"` with the (truncated) error
    text as `reason`, and returned unchanged. A `body` that *raises* is the
    residual, not-yet-sanitised case (D-SANITIZE-UNEXPECTED's last row): the
    exception is converted here to the fixed, generic error string, with the
    full `repr` logged server-side only. Any other return value is treated
    as success, logged `outcome="succeeded"`, and returned unchanged.

    Args:
        action: The tool's own name (e.g. `"ingest_regulation"`), used as
            the log entries' `action` field -- distinct per tool, unlike
            `cypher`'s shared `_ACTION` constant.
        principal: The caller identity `_resolve_principal` already
            resolved; threaded onto every log entry this call emits
            (`"unknown"` when `None`), and available to `body` via the
            closure that constructed it.
        body: A zero-arg callable running the tool's actual work inside the
            bound run context (its own `run_id` is read via
            `current_run_id()`, since binding happens here, not in `body`).

    Returns:
        `body`'s return value unchanged on success or a handled `error:`
        string; the fixed generic error string if `body` raised.
    """
    principal_extra = principal or "unknown"
    with bind_run_context() as run_id:
        emit_log_entry(
            component=_COMPONENT,
            action=action,
            outcome="started",
            run_id=run_id,
            extra={"principal": principal_extra},
        )
        try:
            result = body()
        except Exception as exc:  # noqa: BLE001 -- residual MCP-boundary safety net (D-SANITIZE-UNEXPECTED's last row): body() must never raise across the MCP boundary
            emit_log_entry(
                component=_COMPONENT,
                action=action,
                outcome="failed",
                run_id=run_id,
                extra={"principal": principal_extra, "detail": repr(exc)},
            )
            return _UNEXPECTED_ERROR_MESSAGE
        if isinstance(result, str) and result.startswith("error:"):
            emit_log_entry(
                component=_COMPONENT,
                action=action,
                outcome="failed",
                run_id=run_id,
                extra={"principal": principal_extra, "reason": result[:_STAGE_REASON_MAX_LEN]},
            )
            return result
        emit_log_entry(
            component=_COMPONENT,
            action=action,
            outcome="succeeded",
            run_id=run_id,
            extra={"principal": principal_extra},
        )
        return result


def _sanitize_graph_open[**P, R](opener: Callable[P, R]) -> Callable[P, R]:
    """Wrap one graph opener so any failure sanitises to `McpGraphUnavailableError`.

    Mirrors `_resolve_graph`'s own pattern for the `cypher` tool
    (D-SANITIZE-UNEXPECTED). Host/port/driver detail must not cross the MCP
    boundary from this call site either.
    """

    def _wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return opener(*args, **kwargs)
        except Exception as exc:
            raise McpGraphUnavailableError(_GRAPH_UNAVAILABLE_DETAIL) from exc

    return _wrapped


def _sanitize_pipeline_graph_opens(dependencies: PipelineDependencies) -> PipelineDependencies:
    """Wrap all three of `dependencies.graphs`' openers (D-SANITIZE-UNEXPECTED).

    `run_catalog_ingestion_pipeline` calls `dependencies.graphs.native`/
    `.baseline`/`.single_tenant` directly, with no try/except of its own --
    confirmed by reading it: it opens all three graphs before
    `_execute_catalog_stages`/`_run_stage` ever runs, so a stage failure's own
    `PipelineStageError` sanitisation never covers this earlier step. Without
    this wrapper, an unreachable FalkorDB here would instead reach the caller
    as `_run_mcp_action`'s generic residual error.
    """
    graphs = dependencies.graphs
    return dataclasses.replace(
        dependencies,
        graphs=GraphOpeners(
            native=_sanitize_graph_open(graphs.native),
            baseline=_sanitize_graph_open(graphs.baseline),
            single_tenant=_sanitize_graph_open(graphs.single_tenant),
        ),
    )


def _sanitize_change_check_graph_opens(
    dependencies: ChangeCheckDependencies,
) -> ChangeCheckDependencies:
    """Wrap `open_single_tenant`/`open_native` (D-SANITIZE-UNEXPECTED).

    `run_change_check_sweep` calls `dependencies.open_single_tenant`
    directly, with no try/except of its own, and `_reingest_one` calls
    `dependencies.open_native` the same way when an amendment is
    re-ingested -- confirmed by reading `change_check_orchestration.py` in
    full. Mirrors `_sanitize_pipeline_graph_opens`'s own shape exactly,
    reusing the same per-opener `_sanitize_graph_open` wrapper (not
    reinvented) rather than duplicating its try/except body a third time.
    """
    return dataclasses.replace(
        dependencies,
        open_single_tenant=_sanitize_graph_open(dependencies.open_single_tenant),
        open_native=_sanitize_graph_open(dependencies.open_native),
    )


def _sanitize_near_miss_review_graph_opens(
    dependencies: NearMissReviewDependencies,
) -> NearMissReviewDependencies:
    """Wrap `open_single_tenant_graph` (D-SANITIZE-UNEXPECTED).

    `run_list_near_misses`/`run_resolve_near_miss` both call
    `dependencies.open_single_tenant_graph` directly, with no try/except of
    their own -- confirmed by reading `near_miss_review_orchestration.py` in
    full. Mirrors `_sanitize_pipeline_graph_opens`'s/
    `_sanitize_change_check_graph_opens`'s own shape exactly, reusing the
    same generic per-opener `_sanitize_graph_open` wrapper (not reinvented)
    rather than duplicating its try/except body a third time.
    """
    return dataclasses.replace(
        dependencies,
        open_single_tenant_graph=_sanitize_graph_open(dependencies.open_single_tenant_graph),
    )


def _sanitize_restore_graph_opens(
    dependencies: CatalogRestoreDependencies,
) -> CatalogRestoreDependencies:
    """Wrap `dependencies.open_db` (D-SANITIZE-RESTORE).

    `run_restoration_from_catalog_source` calls `dependencies.open_db`
    directly, with no try/except of its own -- confirmed by reading the
    function's full body. Mirrors `_sanitize_pipeline_graph_opens`'s/
    `_sanitize_change_check_graph_opens`'s/`_sanitize_near_miss_review_graph_opens`'s
    own shape exactly, reusing the same generic per-opener
    `_sanitize_graph_open` wrapper (not reinvented) rather than duplicating
    its try/except body a fourth time. `dependencies.single_tenant_graph_name`
    is a pure name lookup with no I/O of its own (`_default_single_tenant_graph_name`,
    `restore_orchestration.py:400-407`) and needs no wrapping.
    """
    return dataclasses.replace(dependencies, open_db=_sanitize_graph_open(dependencies.open_db))


def _resolve_and_ingest(
    celex: str,
    short_name: str,
    entry: CatalogEntry | None,
    *,
    config: ServiceConfig,
    principal: str | None,
    run_id: str,
) -> dict[str, object] | str:
    """Resolve `entry` (if needed), run the pipeline, and map its exceptions.

    `entry` non-`None` means `celex` is already curated (the caller already
    matched its `short_name`); `None` means it must be resolved against
    Cellar/ELI first (issue #96 -- `short_name` is used verbatim, never
    derived). Any exception this function does not itself catch is the
    residual D-SANITIZE-UNEXPECTED row, left to `_run_mcp_action`'s own
    safety net.
    """
    ingestion_adapter: IngestionAdapter | None = None
    try:
        if entry is None:
            resolution = resolve_via_cellar(celex, short_name=short_name)
            entry = resolution.entry
            ingestion_adapter = resolution.adapter
        outcome = run_catalog_ingestion_pipeline(
            entry,
            config=config,
            run_id=run_id,
            caller=principal or "unknown",
            dependencies=_sanitize_pipeline_graph_opens(build_default_pipeline_dependencies()),
            ingestion_adapter=ingestion_adapter,
        )
    except (
        CatalogIdentifierNotFoundError,
        IngestionConfigIncompleteError,
        PipelineStageError,
    ) as exc:
        return f"error: {exc}"
    except McpGraphUnavailableError:
        return _GRAPH_UNAVAILABLE_MESSAGE
    return _to_accepted_response(run_id, outcome).model_dump()


@server.tool()
def ingest_regulation(
    celex: Annotated[str, Field(min_length=10, max_length=10, pattern=_CELEX_PATTERN)],
    short_name: Annotated[str, Field(pattern=_SHORT_NAME_PATTERN)],
) -> dict[str, object] | str:
    """IngestRegulation: ingest one EU regulation by CELEX into the compliance graph.

    Runs the full external pipeline (Ingestion -> Domain Mapper -> Company
    Merge) for `celex`, in-process, exactly like `POST /ingestions` does for
    a `source: "catalog"` request. `short_name` is always required (issue
    #96): for a CELEX already in the curated catalog, it must equal that
    entry's own canonical short name exactly -- pass a different value and
    the call is rejected with a named error rather than silently
    substituting the catalog's own value. For a CELEX outside the curated
    catalog, `short_name` is resolved against Cellar/ELI and used verbatim
    (issue #96's fix: nothing is derived from the fetched title, so the same
    CELEX ingested twice never forks into two differently-named graphs).

    On success, returns the same structured summary `POST /ingestions`
    returns: `run_id`, `regulatory_instrument_id`, `source`, and one
    `stages` entry per completed pipeline stage (ingestion, extraction,
    derivation, merge) with its own small integer `summary`. Returns a
    string beginning `error: ` when: `short_name` does not match a curated
    CELEX's own value; `celex` exists in neither the curated catalog nor
    Cellar/ELI; the LLM Interface dependency is currently unhealthy (checked
    before any graph is opened or the pipeline is called); the service
    configuration is missing an LLM/embedding model or similarity threshold;
    the policy graph database cannot be reached; a pipeline stage genuinely
    fails mid-run; or (this tool's own residual safety net) on any other
    unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        if not dependency_health.is_healthy(dependency_health.LLM_INTERFACE):
            return _LLM_INTERFACE_UNAVAILABLE_MESSAGE
        entry = find_by_celex(celex)
        if entry is not None and short_name != entry.short_name:
            return (
                f"error: CELEX {celex} is curated under short_name "
                f"'{entry.short_name}'; pass that value, not '{short_name}'"
            )
        # `_run_mcp_action` always binds a run_id via `bind_run_context()` before
        # calling this closure, so `current_run_id()` is never actually `None`
        # here -- the `""` fallback only satisfies the type checker's narrowing,
        # mirroring `_resolve_principal`'s own "unreachable in practice" idiom.
        run_id = current_run_id() or ""
        return _resolve_and_ingest(
            celex, short_name, entry, config=config, principal=principal, run_id=run_id
        )

    return _run_mcp_action("ingest_regulation", principal, _body)


@server.tool()
def check_regulations() -> dict[str, object] | str:
    """CheckRegulations: sweep every tracked regulation for detected amendments.

    Runs the full change-check sweep in-process, exactly like `POST
    /change-checks` does: opens the merged compliance graph, reads every
    actively-tracked external `regulation`/`directive` instrument, polls for
    a newer consolidated version of each, and automatically re-ingests any
    detected amendment (D-DELEGATE). Takes zero parameters -- there is no
    client-supplied input to validate for this tool, so AC-BI-005's
    format-validation surface does not apply here; this is a deliberate
    absence, not a gap.

    On success, returns the same structured summary `POST /change-checks`
    returns: `run_id` and one `instruments` entry per tracked instrument,
    each carrying its own `instrument_id` and `outcome` (`current`,
    `amendment_reingested`, `poll_failed`, `not_configured`, `skipped`, or
    `reingest_failed`), plus `detail`/`reingest_run_id` where applicable.
    Returns a string beginning `error: ` when the LLM Interface dependency
    is currently unhealthy (checked before any graph is opened or the sweep
    is run), when the policy graph database cannot be reached, or (this
    tool's own residual safety net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        if not dependency_health.is_healthy(dependency_health.LLM_INTERFACE):
            return _LLM_INTERFACE_UNAVAILABLE_MESSAGE
        # `_run_mcp_action` always binds a run_id via `bind_run_context()` before
        # calling this closure, so `current_run_id()` is never actually `None`
        # here -- the `""` fallback only satisfies the type checker's narrowing,
        # mirroring `ingest_regulation`'s own identical idiom.
        run_id = current_run_id() or ""
        try:
            result = run_change_check_sweep(
                config=config,
                run_id=run_id,
                dependencies=_sanitize_change_check_graph_opens(
                    build_default_change_check_dependencies()
                ),
            )
        except McpGraphUnavailableError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return _to_change_check_response(result).model_dump()

    return _run_mcp_action("check_regulations", principal, _body)


@server.tool()
def near_misses_list() -> dict[str, object] | str:
    """ListNearMisses: list every unresolved near-miss pending review.

    Runs in-process, exactly like `GET /near-misses` does: opens the merged
    (single-tenant) compliance graph and returns every unresolved
    `PendingReview` -- a Company Merge dedup candidate awaiting a
    keep-separate/merge decision. Takes zero parameters. Unlike
    `ingest_regulation`/`check_regulations`, this tool has no LLM Interface
    dependency of its own (Company Merge's dedup pass runs only during
    ingestion/merge, not during this read), so it does not run the
    LLM-Interface pre-flight check those two tools do -- matching ps-cli's
    own `handle_near_misses_list`, which never calls
    `_assert_llm_interface_available` either.

    On success, returns the same structured summary `GET /near-misses`
    returns: a `reviews` list, one entry per unresolved review, each
    carrying `id`, `kind`, `incoming_text`, `nearest_existing_text`, and
    `similarity` (the two canonical node ids a review references are
    deliberately omitted from this wire shape, matching the REST endpoint).
    Returns a string beginning `error: ` when the policy graph database
    cannot be reached, or (this tool's own residual safety net) on any other
    unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        try:
            result = run_list_near_misses(
                config=config,
                dependencies=_sanitize_near_miss_review_graph_opens(
                    build_default_near_miss_review_dependencies()
                ),
            )
        except McpGraphUnavailableError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return result.model_dump()

    return _run_mcp_action("near_misses_list", principal, _body)


_MERGE_APPROVAL_REQUIRES_AUTHENTICATED_CALLER_MESSAGE = (
    "error: a signed passkey approval requires a real authenticated session"
)


def _resolve_merge_pending_approval(
    review_id: str, ctx: Context, config: ServiceConfig
) -> dict[str, object] | str:
    """`near_misses_resolve`'s `decision="merge"` branch (PLAN.md §2.2 steps 1-4).

    Split out of `near_misses_resolve`'s own body so that function's
    `_body` closure stays within a sane branch/return count -- this is not
    reused by any other tool.
    """
    actor = _resolve_signing_actor()
    if actor is None:
        return _MERGE_APPROVAL_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
    store = PsycopgPendingApprovalStore(config)
    try:
        approval = create_merge_pending_approval(
            review_id=review_id,
            actor=actor,
            base_url=_resolve_base_url(ctx),
            config=config,
            near_miss_dependencies=_sanitize_near_miss_review_graph_opens(
                build_default_near_miss_review_dependencies()
            ),
            store=store,
        )
    except PendingReviewNotFoundError as exc:
        return f"error: {exc}"
    except McpGraphUnavailableError:
        return _GRAPH_UNAVAILABLE_MESSAGE
    return {
        "pending_approval_id": approval.pending_approval_id,
        "approval_url": approval.approval_url,
        "expires_at": approval.expires_at,
    }


@server.tool()
def near_misses_resolve(
    review_id: Annotated[str, Field(min_length=1)],
    decision: Literal["keep-separate", "merge"],
    ctx: Context,
) -> dict[str, object] | str:
    """ResolveNearMiss: resolve one near-miss pending review.

    `decision="keep-separate"` deletes only the `PendingReview` record --
    `winner_id`/`loser_id` stay `None` in the response, unchanged since
    issue #35. `decision`'s `Literal` type is itself the MCP-schema-level
    rejection of any other value, before this tool's body ever runs.

    `decision="merge"` is IRREVERSIBLE once it actually executes -- it
    deletes the loser node and re-points every edge that referenced it onto
    the winner, atomically -- so, since issue #131, this call never executes
    a merge itself. Instead it requires an already-authenticated caller
    (`ctx`'s bound `AccessToken`; a caller under the local-test bypass, which
    has no real actor identity, is refused) and returns a pending,
    signed-passkey approval immediately: `pending_approval_id`,
    `approval_url` (a link a human opens in a browser to complete a WebAuthn
    signing ceremony), and `expires_at`. No graph write happens on this
    call at all -- `near_misses_check_approval` is the separate, resumable
    way to learn whether that approval has since been signed. The REST
    route `POST /near-misses/{review_id}/resolve` calls the exact same
    underlying function for `decision="merge"`, so there is never a second,
    parallel gating mechanism.

    Like `near_misses_list`, this tool has no LLM Interface dependency of
    its own and so runs no LLM-Interface pre-flight check.

    On success, returns `{"review_id", "decision", "winner_id": None,
    "loser_id": None}` for `decision="keep-separate"`, or
    `{"pending_approval_id", "approval_url", "expires_at"}` for
    `decision="merge"`. Returns a string beginning `error: ` when
    `review_id` doesn't exist or was already resolved, when (`merge` only)
    the caller has no real authenticated session, when the policy graph
    database cannot be reached, or (this tool's own residual safety net) on
    any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        if decision == "merge":
            return _resolve_merge_pending_approval(review_id, ctx, config)
        try:
            result = run_resolve_near_miss(
                review_id,
                decision,
                config=config,
                dependencies=_sanitize_near_miss_review_graph_opens(
                    build_default_near_miss_review_dependencies()
                ),
            )
        except PendingReviewNotFoundError as exc:
            return f"error: {exc}"
        except McpGraphUnavailableError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return result.model_dump()

    return _run_mcp_action("near_misses_resolve", principal, _body)


@server.tool()
def near_misses_check_approval(
    pending_approval_id: Annotated[str, Field(min_length=1)],
) -> dict[str, object] | str:
    """CheckApproval: resumable status check for a near-miss merge's pending approval (issue #131).

    Companion to `near_misses_resolve`'s `decision="merge"` branch (PLAN.md
    §2.3): that call never blocks waiting for a human to complete a WebAuthn
    signing ceremony, so this is the separate, poll-again-later way to learn
    whether it has been signed yet. **Ownership check**: only the same
    caller who created the approval may check it -- a caller with no real
    authenticated session, or one whose identity doesn't match the
    approval's own, gets the identical generic not-found error a wrong id
    gets (never distinguished, so a caller cannot learn whether an id
    belongs to someone else).

    On success, returns `{"pending_approval_id", "status", "review_id",
    "decision", "winner_id", "loser_id"}`. `status` is one of `"pending"`,
    `"expired"` (the 15-minute window elapsed unsigned -- derived live, never
    a separately stored status), or `"signed"`; `winner_id`/`loser_id`
    populate only once `status == "signed"`. Returns a string beginning
    `error: ` when no such pending approval is visible to this caller, or
    (this tool's own residual safety net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        actor = _resolve_signing_actor()
        store = PsycopgPendingApprovalStore(config)
        status = check_pending_approval(
            pending_approval_id=pending_approval_id, actor=actor, store=store
        )
        if status is None:
            return f"error: no pending approval with id {pending_approval_id!r}"
        return {
            "pending_approval_id": status.pending_approval_id,
            "status": status.status,
            "review_id": status.review_id,
            "decision": status.decision,
            "winner_id": status.winner_id,
            "loser_id": status.loser_id,
        }

    return _run_mcp_action("near_misses_check_approval", principal, _body)


@server.tool(name="set-catalog-source")
def set_catalog_source(url: Annotated[str, Field(min_length=1)]) -> dict[str, object] | str:
    """SetCatalogSource: override the effective curated-content source (issue #125, AC-BI-012).

    Validates `url` against the exact same http(s)/TLS rules startup
    configuration uses (`ps_service.curated_source.source_url.
    validate_source_url` -- AC-BI-008/010): only `http(s)://` schemes are
    accepted, and a plain `http://` URL is rejected unless
    `PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP` is set for this process. On
    success, persists `url` in FalkorDB as the effective curated-content
    source -- no restart required -- and it takes precedence over
    `PS_CURATEDSOURCE_URL`/the public default on every subsequent
    `GET /catalog` and artifact fetch (AC-BI-013), until `reset-catalog-source`
    is called.

    Since issue #133, requires the caller hold `SystemAdmin` or above
    (`ps_service.authz.service.require_role`) -- skipped entirely under the
    local-test bypass (PLAN.md §3.3), which never sees a real actor
    identity to check.

    On success, returns `{"url": <the validated url>, "source": "override"}`.
    Returns a string beginning `error: ` when the caller lacks the required
    access role, when `url` fails validation, when the policy graph database
    cannot be reached, or (this tool's own residual safety net) on any other
    unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        if not config.is_local_test_bypass_active:
            if actor is None:
                return _CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
            try:
                require_role(
                    actor,
                    minimum=AccessRole.SYSTEM_ADMIN,
                    store=PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config)),
                )
            except (AccessDeniedError, AuthorizationStoreUnavailableError) as exc:
                return f"error: {exc}"
        try:
            validated_url = validate_source_url(
                url, allow_insecure_http=config.curated_source_allow_insecure_http
            )
        except CuratedSourceConfigurationError as exc:
            return f"error: {exc}"
        try:
            graph = _resolve_graph(config)
            catalog_source_store.set_override(graph, validated_url)
        except McpGraphUnavailableError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return {"url": validated_url, "source": "override"}

    return _run_mcp_action("set_catalog_source", principal, _body)


@server.tool(name="reset-catalog-source")
def reset_catalog_source() -> dict[str, object] | str:
    """ResetCatalogSource: clear the persisted curated-content source override (AC-BI-014).

    Takes no parameters. On success, deletes the persisted FalkorDB override
    (a no-op if none was set) -- the effective source immediately reverts to
    `PS_CURATEDSOURCE_URL`/the public default on every subsequent
    `GET /catalog` and artifact fetch, no restart required.

    Since issue #133, requires the caller hold `SystemAdmin` or above
    (`ps_service.authz.service.require_role`) -- skipped entirely under the
    local-test bypass (PLAN.md §3.3), which never sees a real actor
    identity to check.

    On success, returns `{"url": <the env-var/default url>, "source": "default"}`.
    Returns a string beginning `error: ` when the caller lacks the required
    access role, when the policy graph database cannot be reached, or (this
    tool's own residual safety net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        if not config.is_local_test_bypass_active:
            if actor is None:
                return _CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
            try:
                require_role(
                    actor,
                    minimum=AccessRole.SYSTEM_ADMIN,
                    store=PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config)),
                )
            except (AccessDeniedError, AuthorizationStoreUnavailableError) as exc:
                return f"error: {exc}"
        try:
            graph = _resolve_graph(config)
            catalog_source_store.reset_override(graph)
        except McpGraphUnavailableError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return {"url": config.curated_source_base_url, "source": "default"}

    return _run_mcp_action("reset_catalog_source", principal, _body)


@server.tool(name="get-catalog-source")
def get_catalog_source() -> dict[str, object] | str:
    """GetCatalogSource: report the currently effective curated-content source (AC-BI-015).

    Takes no parameters. Checks for a persisted FalkorDB override first,
    falling back to `PS_CURATEDSOURCE_URL`/the public default when none is
    set, OR when the policy graph database is unreachable for that check
    (D-FAILOPEN) -- this tool never fails on a FalkorDB outage; it simply
    reports the fallback source.

    Since issue #133, requires the caller hold `SystemAdmin` or above
    (`ps_service.authz.service.require_role`) -- skipped entirely under the
    local-test bypass (PLAN.md §3.3), which never sees a real actor
    identity to check.

    On success, returns `{"url": <the effective url>, "source": "override"}`
    when a persisted override is in effect, or `{"url": ..., "source":
    "default"}` otherwise. Returns a string beginning `error: ` when the
    caller lacks the required access role, or (this tool's own residual
    safety net) on any other unexpected failure unrelated to the FalkorDB
    override check, which always fails open rather than erroring.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        if not config.is_local_test_bypass_active:
            if actor is None:
                return _CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
            try:
                require_role(
                    actor,
                    minimum=AccessRole.SYSTEM_ADMIN,
                    store=PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config)),
                )
            except (AccessDeniedError, AuthorizationStoreUnavailableError) as exc:
                return f"error: {exc}"
        effective = resolve_effective_source(config, open_graph=lambda: _resolve_graph(config))
        return {"url": effective.url, "source": "override" if effective.is_override else "default"}

    return _run_mcp_action("get_catalog_source", principal, _body)


def _emit_invite_created_audit_entry(
    *, actor: tuple[str, str] | None, principal: str | None, email: str
) -> None:
    """AC-BI-009's audit trail: record actor, target email, timestamp, outcome.

    `_run_mcp_action` (below) already emits the generic `component=
    "mcp_interface"` started/succeeded/failed triad for every tool call,
    including `invite_user` -- that already carries `principal` and a
    `timestamp` (`LogEntry`'s default factory) on every call. AC-BI-009
    additionally names the **target email** specifically, which that
    generic triad's fixed `extra={"principal": ...}` shape does not carry.
    This is a component-specific *additional* emission alongside that
    triad, not a replacement for it -- mirrors `ps_service.authz.service`'s
    `_maybe_log_system_owner_floor_warning` (same "extra, scoped log next
    to the generic one" shape).

    Called from `invite_user`'s `_body()` only on the success path, after
    `create_invitation` returns and before the result is returned.

    PII note: `ps_service.logging.models`'s module docstring says the
    caller owns PII hygiene in `extra` and to never place PII there. Putting
    the invitee's email in `entity_id` (and the actor's identity in `extra`)
    is a deliberate, scoped exception to that general guidance: AC-BI-009
    explicitly requires the target email in this audit trail -- an
    admin-invite audit record with no target identity would be useless.

    Swallows `LoggingLifecycleError` (mirrors
    `_maybe_log_system_owner_floor_warning`'s own `contextlib.suppress`): a
    missing log sink must never turn an otherwise-successful invite into a
    failed tool call.

    Args:
        actor: The verified `(sub, iss)` pair `_resolve_authz_actor` already
            resolved for this call, or `None` under the local-test bypass.
        principal: `_resolve_principal`'s own resolved identity, used as the
            fallback actor label when `actor` is `None` (bypass).
        email: The invitee's target email address -- this call's
            `entity_id`.
    """
    with contextlib.suppress(LoggingLifecycleError):
        emit_log_entry(
            component="invitations",
            action="invite_user",
            entity_id=email,
            outcome="created",
            extra={"actor": actor[0] if actor is not None else (principal or "unknown")},
        )


@server.tool(name="invite-user")
def invite_user(
    email: Annotated[str, Field(min_length=3, pattern=_INVITE_EMAIL_PATTERN)],
) -> dict[str, object] | str:
    """InviteUser: create a single-use Authentik enrollment invite for `email` (issue #140).

    Requires the caller hold `SystemAdmin` or above (`ps_service.authz.
    service.require_role`) -- skipped entirely under the local-test bypass,
    mirroring `set-catalog-source`'s exact gate (issue #133). Uses PS
    Service's own configured `PS_AUTHENTIK_API_TOKEN` service credential --
    never a caller-supplied token -- to call Authentik's invitation-stage
    API.

    On success, returns `{"itoken": <pk>, "invite_url": <redemption URL>}`.
    Returns a string beginning `error: ` when the caller lacks the required
    access role, when `email` is not a plausible address (rejected at the
    MCP schema layer, before this tool's body ever runs), when Authentik is
    unreachable or returns a non-2xx response, or (this tool's own residual
    safety net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        if not config.is_local_test_bypass_active:
            if actor is None:
                return _CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
            try:
                require_role(
                    actor,
                    minimum=AccessRole.SYSTEM_ADMIN,
                    store=PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config)),
                )
            except (AccessDeniedError, AuthorizationStoreUnavailableError) as exc:
                return f"error: {exc}"
        try:
            result = create_invitation(config, email)
        except AuthentikInvitationError as exc:
            return f"error: {exc}"
        _emit_invite_created_audit_entry(actor=actor, principal=principal, email=email)
        return {"itoken": result.itoken, "invite_url": result.invite_url}

    return _run_mcp_action("invite_user", principal, _body)


@server.tool(name="get-catalog-listing")
def get_catalog_listing() -> dict[str, object] | str:
    """GetCatalogListing: list every curated instrument (external and internal), issue #127.

    Runs in-process, exactly like `GET /catalog` does (D-CATALOG-NO-ORCH-LAYER
    -- there is no separate orchestration module for this route to delegate
    to, so this tool replicates `list_curated_catalog`'s own two-call
    sequence and response mapping inline): resolves the effective
    curated-content source (a persisted FalkorDB override when one exists,
    else the configured env-var/default -- D-FAILOPEN, so this tool never
    fails on a FalkorDB outage during that check), then fetches and parses
    `catalog.json` from it. Takes zero parameters -- there is no
    client-supplied input to validate, so AC-BI-005's format-validation
    surface does not apply here, a deliberate absence mirroring
    `check_regulations`/`near_misses_list`.

    On success, returns the same structured listing `GET /catalog` returns:
    an `instruments` list, one entry per curated instrument (external and
    internal, unfiltered), each carrying `instrument_id`, `title`,
    `source_type`, and `jurisdiction` (`None` for an internal-source entry).
    Returns a string beginning `error: ` when the configured curated-content
    source is unreachable or returns a missing/malformed `catalog.json`, or
    (this tool's own residual safety net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        dependencies: CuratedCatalogDependencies = build_default_curated_catalog_dependencies()
        effective_source = dependencies.resolve_effective_source(config)
        try:
            entries = dependencies.fetch_catalog(effective_source.url)
        except CuratedSourceFetchError as exc:
            return f"error: {exc}"
        return CuratedCatalogResponse(
            instruments=[
                CatalogInstrumentEntry(
                    instrument_id=entry.instrument_id,
                    title=entry.title,
                    source_type=entry.source_type,
                    jurisdiction=entry.jurisdiction,
                )
                for entry in entries
            ]
        ).model_dump()

    return _run_mcp_action("get_catalog_listing", principal, _body)


@server.tool()
def restore_instrument(
    instrument_id: Annotated[
        str,
        Field(pattern=_RESTORE_INSTRUMENT_ID_PATTERN),
        AfterValidator(_reject_path_traversal_segment),
    ],
) -> dict[str, object] | str:
    """RestoreInstrumentFromCatalog: fetch and restore a curated instrument's artifact (#127).

    Runs in-process, exactly like `POST /restorations/from-catalog` does
    (D-RESTORE-DELEGATE): fetches `instrument_id`'s manifest/baseline/native
    artifact from the effective curated-content source (a persisted
    FalkorDB override when one exists, else the configured env-var/default),
    then restores it into the policy graph -- delegating directly to
    `run_restoration_from_catalog_source`, the exact same function the REST
    route calls, never reimplemented. `instrument_id` is validated against
    the same charset/length bound ps-cli's own `restore instrument`
    positional used, plus its explicit rejection of any `".."` substring
    (AC-BI-005, D-INSTRUMENT-ID-STRICTNESS) -- rejected at the MCP schema
    layer, before this tool's body ever runs.

    On success, returns the same structured summary ps-cli's `restore
    instrument` used to print: `instrument_id` and one `stages` entry per
    completed restore stage, each carrying its own `stage`/`status`
    (AC-BI-004). Returns a string beginning `error: ` when the configured
    curated-content source is unreachable or the fetched artifact is
    missing/malformed, when the fetched artifact fails checksum/
    schema_version verification, when any other restore stage genuinely
    fails (including a missing `PS_COMPANYMERGE_SIMILARITY_THRESHOLD`
    configuration value), when the policy graph database cannot be reached,
    or (this tool's own residual safety net) on any other unexpected
    failure.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        request_body = CatalogRestorationRequest(instrument_id=instrument_id)
        dependencies: CatalogRestoreDependencies = _sanitize_restore_graph_opens(
            build_default_restore_from_catalog_dependencies()
        )
        try:
            outcome = run_restoration_from_catalog_source(
                request_body,
                config=config,
                actor=principal or "unknown",
                dependencies=dependencies,
            )
        except (
            CuratedSourceUnavailableError,
            RestoreArtifactRejectedError,
            RestoreStageFailedError,
        ) as exc:
            return f"error: {exc}"
        except McpGraphUnavailableError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return outcome.model_dump()

    return _run_mcp_action("restore_instrument", principal, _body)


# D2 (issue #139): `celex` is a plain node PROPERTY on `RegulatoryInstrument`,
# never its node id (`{SHORT}-{VERSION}`) -- confirmed against
# `ingestion/graph_writer.py`'s `register_regulatory_instrument_version`.
# Fixed, code-owned, never string-interpolated -- `celex_ids` always flows in
# via `params` (L2 Query Safety).
_INGESTION_STATUS_QUERY = (
    "UNWIND $celex_ids AS c "
    "MATCH (n:RegulatoryInstrument {celex: c}) "
    "RETURN DISTINCT n.celex AS celex"
)


@server.tool()
def check_instrument_ingestion_status(
    celex_ids: Annotated[
        list[Annotated[str, Field(min_length=10, max_length=10, pattern=_CELEX_PATTERN)]],
        Field(min_length=1, max_length=25),
    ],
) -> dict[str, object] | str:
    """CheckInstrumentIngestionStatus: report whether each given EU instrument is already ingested.

    For every CELEX id in `celex_ids` (1-25 items, each a well-formed EU
    CELEX identifier -- same pattern `ingest_regulation`'s own `celex`
    parameter uses, rejected at the MCP schema layer before this tool's body
    ever runs if malformed or if the list is empty or too long), checks the
    policy graph for a `RegulatoryInstrument` node carrying that CELEX as its
    `celex` property (a plain property, distinct from that node's own
    `{SHORT}-{VERSION}` id) and reports whether it has already been
    ingested.

    On success, returns `{"statuses": {<celex>: "ingested" | "not_yet_ingested"
    | "unknown", ...}}`, one entry per requested `celex_ids` element
    (duplicate ids collapse via dict semantics). Intended for a Compliance
    Officer's applicability assessment: batch every candidate instrument's
    CELEX into one call to learn which ones the graph already tracks,
    rather than calling `cypher` once per candidate.

    Issue #139, D5/S5 (AC-BI-009): a graph-availability problem never fails
    the whole call -- unlike `cypher`/`check_regulations`/`near_misses_list`,
    which all return a bare `error: ...` string and abort on
    `McpGraphUnavailableError`/`QueryEngineExecutionError`. Here, every one
    of those (plus `GraphUnseededError` and the practically-unreachable
    `WriteClauseRejectedError`, caught for completeness since this query is
    fixed and code-owned) instead folds every requested `celex_ids` entry to
    `"unknown"`, per AC-BI-009's literal wording ("the artifact still
    returns the candidate list ... rather than the whole assessment
    failing"). This deliberately includes a totally unseeded graph
    (`GraphUnseededError`): PLAN.md D5 reads a fresh/unseeded graph as
    `"unknown"` rather than confidently `"not_yet_ingested"`, reasoning that
    an unseeded graph is at least as likely to signal a real
    provisioning/connectivity problem as a genuinely fresh company graph.
    The call itself still completes successfully (`_run_mcp_action` logs
    `outcome="succeeded"`, never `"failed"`) -- this degraded response is a
    dict, never a top-level `error:` string, so it never trips
    `_run_mcp_action`'s failed-outcome branch.

    This tool requires no elevated access role, matching `cypher`/
    `check_regulations`/`near_misses_list` -- it is architecturally a plain
    graph read, not an administrative write, so it sits on the same
    un-gated `server` singleton every other tool does; per-caller bearer-
    token authentication is still enforced uniformly by the HTTP transport
    for every registered tool (issue #139, D6).
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object]:
        try:
            graph = _resolve_graph(config)
            result = execute_cypher_query(
                _INGESTION_STATUS_QUERY,
                graph=graph,
                principal=principal,
                timeout_ms=config.query_timeout_ms,
                row_cap=config.query_row_cap,
                params={"celex_ids": celex_ids},
            )
        except (
            McpGraphUnavailableError,
            GraphUnseededError,
            QueryEngineExecutionError,
            WriteClauseRejectedError,
        ):
            return {"statuses": dict.fromkeys(celex_ids, "unknown")}
        ingested = {row[0] for row in result.rows}
        return {
            "statuses": {
                celex: "ingested" if celex in ingested else "not_yet_ingested"
                for celex in celex_ids
            }
        }

    return _run_mcp_action("check_instrument_ingestion_status", principal, _body)


@server.tool()
def cypher(query: str) -> dict[str, object] | str:
    """Run a read-only, MATCH/RETURN-shaped Cypher query against the policy_system graph.

    On success returns an object with `columns`, `rows`, `row_count`, and
    `truncated` (`true` when more rows matched than the configured row cap
    returned). Returns a string beginning `error: ` when the query contains
    a write clause (CREATE, MERGE, DELETE, SET, REMOVE, DROP, FOREACH --
    rejected before execution), when the graph has no seeded content at all
    yet (distinct from a query that legitimately matches nothing, which
    still returns the normal empty-result shape), when FalkorDB rejects the
    query, or when the graph database cannot be reached.
    """
    try:
        config = load_config()
        graph = _resolve_graph(config)
    except McpGraphUnavailableError, ServiceConfigurationError:
        emit_log_entry(component=_COMPONENT, action=_ACTION, outcome="unavailable")
        return _GRAPH_UNAVAILABLE_MESSAGE
    principal = _resolve_principal(config)
    return handle_mcp_tool_call(
        query,
        graph=graph,
        principal=principal,
        timeout_ms=config.query_timeout_ms,
        row_cap=config.query_row_cap,
    )


@server.tool()
def domain_concepts() -> str:
    """Return the PS compliance-graph vocabulary and schema (ps-domain-concepts.md) verbatim.

    The same text the `psdomain://concepts` resource serves, exposed as a
    tool because some MCP hosts (Claude Desktop among them) let the model
    call tools but not read resources. Takes no parameters. Returns a
    string beginning `error: ` when the backing file cannot be read.
    """
    try:
        return read_domain_concepts()
    except McpResourceUnavailableError as exc:
        return f"error: {exc}"


@server.resource(
    _DOMAIN_CONCEPTS_URI,
    name="ps-domain-concepts",
    title="PS domain concepts",
    description="The canonical PS compliance-graph vocabulary and schema, served verbatim.",
    mime_type="text/markdown",
)
def read_domain_concepts() -> str:
    """GetDomainConcepts: return the full ps-domain-concepts.md text.

    Takes no parameters -- no client-supplied input reaches the read
    (AC-012). Resolves from a repo checkout only; raises a
    resource-unavailable error if the file cannot be read.
    """
    try:
        return _domain_concepts_path().read_text(encoding="utf-8")
    except OSError as exc:
        raise McpResourceUnavailableError(_DOMAIN_CONCEPTS_UNAVAILABLE_DETAIL) from exc


def _assignment_to_dict(row: AccessRoleAssignmentRow) -> dict[str, object]:
    """Shape one `AccessRoleAssignmentRow` for `list-access-roles`'s wire response."""
    return {
        "principal_subject": row.principal_subject,
        "principal_issuer": row.principal_issuer,
        "access_role": row.access_role.value,
        "granted_at": row.granted_at.isoformat(),
        "granted_by_subject": row.granted_by_subject,
        "granted_by_issuer": row.granted_by_issuer,
    }


@server.tool(name="list-access-roles")
def list_access_roles() -> dict[str, object] | str:
    """ListAccessRoles: report every principal's current AccessRole assignments (issue #133).

    Takes no parameters. The very first-ever caller, of any identity, is
    auto-bootstrapped to `AuthenticatedUser` + `SystemOwner` (AC-BI-001)
    before this tool's own gate is evaluated, so that one call always
    succeeds; every later principal defaults to `AuthenticatedUser` alone
    (AC-BI-002) and this tool then requires `SystemAdmin` or `SystemOwner`
    to proceed (the full roster is sensitive -- a plan-original design
    choice, not derived from any specific AC).

    On success, returns `{"assignments": [{"principal_subject",
    "principal_issuer", "access_role", "granted_at", "granted_by_subject",
    "granted_by_issuer"}, ...], "system_owner_floor_warning": bool}`.
    Returns a string beginning `error: ` when the caller has no real
    authenticated session (the local-test bypass included -- access-role
    management is never available under it), when the caller lacks the
    required role, when the authorization store cannot be reached, or (this
    tool's own residual safety net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _ACCESS_ROLE_MANAGEMENT_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
        try:
            result = list_assignments(actor, store=store)
        except (AccessDeniedError, AuthorizationStoreUnavailableError) as exc:
            return f"error: {exc}"
        return {
            "assignments": [_assignment_to_dict(row) for row in result.assignments],
            "system_owner_floor_warning": result.system_owner_floor_warning,
        }

    return _run_mcp_action("list_access_roles", principal, _body)


_GRANT_REVOKE_ERRORS = (
    InvalidAccessRoleError,
    AccessDeniedError,
    SelfGrantOrRevokeBlockedError,
    SystemOwnerFloorViolationError,
    AuthorizationStoreUnavailableError,
)


@server.tool(name="grant-access-role")
def grant_access_role(
    principal_subject: Annotated[str, Field(min_length=1)],
    access_role: Literal["SystemOwner", "SystemAdmin", "PolicyManager"],
) -> dict[str, object] | str:
    """GrantAccessRole: grant `access_role` to `principal_subject` (issue #133).

    RBAC (PLAN.md §0.7, widened per CHANGES.md Appendix A): granting
    `SystemAdmin` or `SystemOwner` requires the caller hold `SystemOwner`;
    granting `PolicyManager` requires the caller hold `SystemOwner` or
    `SystemAdmin`. A caller may never grant a role to themselves
    (AC-BI-005).

    On success, returns `{"principal_subject", "access_role",
    "granted_by_subject", "system_owner_floor_warning"}`. Returns a string
    beginning `error: ` when the caller has no real authenticated session
    (the local-test bypass included -- access-role management is never
    available under it), when `access_role` is not one of the three
    grantable roles, when the caller lacks the required role, when the
    target is the caller themselves, when the authorization store cannot be
    reached, or (this tool's own residual safety net) on any other
    unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)
    issuer = config.auth_issuer

    def _body() -> dict[str, object] | str:
        if actor is None or issuer is None:
            return _ACCESS_ROLE_MANAGEMENT_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
        try:
            result = grant_role(
                actor=actor,
                target_subject=principal_subject,
                access_role=access_role,
                store=store,
                issuer=issuer,
            )
        except _GRANT_REVOKE_ERRORS as exc:
            return f"error: {exc}"
        return {
            "principal_subject": principal_subject,
            "access_role": access_role,
            "granted_by_subject": actor[0],
            "system_owner_floor_warning": result.system_owner_floor_warning,
        }

    return _run_mcp_action("grant_access_role", principal, _body)


@server.tool(name="revoke-access-role")
def revoke_access_role(
    principal_subject: Annotated[str, Field(min_length=1)],
    access_role: Literal["SystemOwner", "SystemAdmin", "PolicyManager"],
) -> dict[str, object] | str:
    """RevokeAccessRole: revoke `access_role` from `principal_subject` (issue #133).

    RBAC (PLAN.md §0.9, widened per CHANGES.md Appendix A): revoking
    `SystemOwner` requires the caller hold `SystemOwner` **or**
    `SystemAdmin` -- once a second `SystemOwner` exists (via
    `grant-access-role`), a `SystemAdmin` can revoke one without the
    self-revoke block ever intervening. Revoking `SystemAdmin`/
    `PolicyManager` mirrors `grant-access-role`'s own actor requirement for
    each role. A caller may never revoke a role from themselves
    (AC-BI-005), checked before the `SystemOwner` floor check (AC-BI-006):
    revoking the last remaining active `SystemOwner` is rejected regardless
    of who the caller is.

    On success, returns `{"principal_subject", "access_role",
    "revoked_by_subject", "system_owner_floor_warning"}`. Returns a string
    beginning `error: ` when the caller has no real authenticated session
    (the local-test bypass included), when `access_role` is not one of the
    three roles this tool manages, when the caller lacks the required role,
    when the target is the caller themselves, when revoking `SystemOwner`
    would leave zero active `SystemOwner`s, when the authorization store
    cannot be reached, or (this tool's own residual safety net) on any other
    unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)
    issuer = config.auth_issuer

    def _body() -> dict[str, object] | str:
        if actor is None or issuer is None:
            return _ACCESS_ROLE_MANAGEMENT_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
        try:
            result = revoke_role(
                actor=actor,
                target_subject=principal_subject,
                access_role=access_role,
                store=store,
                issuer=issuer,
            )
        except _GRANT_REVOKE_ERRORS as exc:
            return f"error: {exc}"
        return {
            "principal_subject": principal_subject,
            "access_role": access_role,
            "revoked_by_subject": actor[0],
            "system_owner_floor_warning": result.system_owner_floor_warning,
        }

    return _run_mcp_action("revoke_access_role", principal, _body)


def _audit_event_to_dict(row: AuditEventRow) -> dict[str, object]:
    """Shape one `AuditEventRow` for `list-audit-events`'s wire response."""
    return {
        "id": row.id,
        "occurred_at": row.occurred_at.isoformat(),
        "actor_subject": row.actor_subject,
        "actor_issuer": row.actor_issuer,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "outcome": row.outcome,
        "details": row.details,
    }


_LIST_AUDIT_EVENTS_ERRORS = (
    AccessDeniedError,
    InvalidAuditQueryFilterError,
    AuthorizationStoreUnavailableError,
)


@server.tool(name="list-audit-events")
def list_audit_events(  # noqa: PLR0913, PLR0917 -- every parameter is an independent, optional query filter (AC-BI-007); collapsing them into one payload object would just move the same count behind a wrapper
    actor_subject: Annotated[str, Field(min_length=1)] | None = None,
    actor_issuer: Annotated[str, Field(min_length=1)] | None = None,
    resource_type: Annotated[str, Field(min_length=1)] | None = None,
    resource_id: Annotated[str, Field(min_length=1)] | None = None,
    action: Annotated[str, Field(min_length=1)] | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    cursor: Annotated[str, Field(min_length=1)] | None = None,
    page_size: Annotated[int, Field(gt=0, le=100)] = 25,
) -> dict[str, object] | str:
    """ListAuditEvents: read the shared `audit_events` audit trail, filtered and paginated.

    Issue #147.

    `SystemOwner`/`SystemAdmin`-gated, mirroring `list-access-roles`'s own
    gate exactly -- the audit trail is at least as sensitive as the roster
    it partly documents. Every parameter is optional and independently
    combinable: `actor_subject`/`actor_issuer` narrow by who acted,
    `resource_type`/`resource_id` by what was acted on, `action` by the
    namespaced action string (e.g. `"access_role.grant"`), `occurred_from`/
    `occurred_to` (ISO 8601) by a time range. Results are always newest
    first. `cursor` (from a prior call's own `next_cursor`) advances to the
    next page; `page_size` bounds how many events one call returns (default
    25, maximum 100).

    On success, returns `{"events": [{"id", "occurred_at", "actor_subject",
    "actor_issuer", "action", "resource_type", "resource_id", "outcome",
    "details"}, ...], "next_cursor": str | None}` -- `next_cursor` is
    `None` once no further events remain. Returns a string beginning
    `error: ` when the caller has no real authenticated session (the
    local-test bypass included -- audit-trail access is never available
    under it), when the caller lacks the required role, when a filter,
    `page_size`, or `cursor` is invalid, when the authorization store
    cannot be reached, or (this tool's own residual safety net) on any
    other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _ACCESS_ROLE_MANAGEMENT_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        access_role_store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
        audit_store = PsycopgAuditStore(config)
        filters = AuditQueryFilters(
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )
        try:
            page = run_list_audit_events(
                actor,
                filters=filters,
                cursor=cursor,
                page_size=page_size,
                access_role_store=access_role_store,
                audit_store=audit_store,
            )
        except _LIST_AUDIT_EVENTS_ERRORS as exc:
            return f"error: {exc}"
        return {
            "events": [_audit_event_to_dict(row) for row in page.events],
            "next_cursor": page.next_cursor,
        }

    return _run_mcp_action("list_audit_events", principal, _body)
