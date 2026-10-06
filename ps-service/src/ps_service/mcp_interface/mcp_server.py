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
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Literal, cast

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import (
    Context,  # noqa: TC002 -- the MCP SDK's own Context-injection resolves this annotation at runtime
)
from pydantic import AfterValidator, Field

from ps_service import dependency_health
from ps_service.api.change_check_orchestration import (
    build_default_change_check_dependencies,
    run_change_check_sweep,
)
from ps_service.api.errors import (
    AccessDeniedError,
    AuthorizationStoreUnavailableError,
    CatalogIdentifierNotFoundError,
    CatalogSourceOverrideUnavailableError,
    CelexAlreadyIngestedError,
    CuratedSourceUnavailableError,
    IngestionConfigIncompleteError,
    InvalidAccessRoleError,
    InvalidAuditQueryFilterError,
    PendingReviewNotFoundError,
    PipelineStageError,
    RestoreArtifactRejectedError,
    RestoreInstrumentIdAmbiguousError,
    RestoreInstrumentIdNotFoundError,
    RestoreStageFailedError,
    SelfGrantOrRevokeBlockedError,
    ShortNameCollisionError,
    SystemOwnerFloorViolationError,
)
from ps_service.api.ingestion_orchestration import (
    _STAGE_REASON_MAX_LEN,  # pyright: ignore[reportPrivateUsage]  -- shared failure-reason cap; D-AUDIT-WRAPPER reuses it for `_run_mcp_action`'s own truncation, mirrors change_check_orchestration.py's own cross-module private-import convention
    GraphOpeners,
    build_default_pipeline_dependencies,
    resolve_ingestion_entry,
    run_catalog_ingestion_pipeline,
)
from ps_service.api.ingestion_orchestration import (
    SHORT_NAME_PATTERN as _SHORT_NAME_PATTERN,  # shared w/ api/models.py's short_name (#146)
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
from ps_service.audit.errors import AuditPersistenceError, AuditPostgresUnavailableError
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
from ps_service.curated_source.errors import CuratedSourceFetchError
from ps_service.curated_source.resolve import resolve_effective_source
from ps_service.graph_cleanup.dependencies import (
    GraphCleanupDependencies,
    build_default_graph_cleanup_dependencies,
)
from ps_service.graph_cleanup.discovery import resolve_min_similarity
from ps_service.graph_cleanup.errors import (
    GraphCleanupAcknowledgmentRequiredError,
    GraphCleanupPersistenceError,
    GraphCleanupValidationError,
)
from ps_service.graph_cleanup.service import (
    check_cleanup_approval,
    create_capability_merge_approval,
    create_obligation_merge_approval,
    create_release_governance_approval,
    create_unmerge_approval,
    find_capability_merge_candidates,
    find_duplicate_obligations,
)
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
from ps_service.policy_lifecycle.errors import (
    PolicyCapabilityAlreadyGovernedError,
    PolicyCapabilityNotFoundError,
    PolicyControlNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyGovernanceConflictError,
    PolicyIncompleteForProposalError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
    PolicyNotFoundError,
    PolicySelfApprovalBlockedError,
    PolicyStandardNotFoundError,
    PolicySupersedePriorNotApprovedError,
    PolicyTitleAlreadyExistsError,
)
from ps_service.policy_lifecycle.service import (
    _CONTROL_IMPLEMENTATION_STATUS_VALUES,  # pyright: ignore[reportPrivateUsage] -- issue #136 Slice 4: same drift-avoidance reuse as `_POLICY_PATCHABLE_FIELDS`
    _CONTROL_PATCHABLE_FIELDS,  # pyright: ignore[reportPrivateUsage] -- issue #136 Slice 4: same drift-avoidance reuse as `_POLICY_PATCHABLE_FIELDS`
    _POLICY_PATCHABLE_FIELDS,  # pyright: ignore[reportPrivateUsage] -- issue #136 PLAN.md §1.7: declared once in service.py, imported here to avoid drift between the two layers
    _STANDARD_IMPLEMENTATION_STATUS_VALUES,  # pyright: ignore[reportPrivateUsage] -- issue #136 Slice 2: same drift-avoidance reuse as `_POLICY_PATCHABLE_FIELDS`
    _STANDARD_PATCHABLE_FIELDS,  # pyright: ignore[reportPrivateUsage] -- issue #136 Slice 2: same drift-avoidance reuse as `_POLICY_PATCHABLE_FIELDS`
    ControlDraftInput,
    StandardDraftInput,
)
from ps_service.policy_lifecycle.service import (
    add_control_to_draft as run_add_control_to_draft,
)
from ps_service.policy_lifecycle.service import (
    add_standard_to_draft as run_add_standard_to_draft,
)
from ps_service.policy_lifecycle.service import (
    approve_policy as run_approve_policy,
)
from ps_service.policy_lifecycle.service import (
    create_policy_draft as run_create_policy_draft,
)
from ps_service.policy_lifecycle.service import (
    get_policy as run_get_policy,
)
from ps_service.policy_lifecycle.service import (
    propose_policy as run_propose_policy,
)
from ps_service.policy_lifecycle.service import (
    reject_policy as run_reject_policy,
)
from ps_service.policy_lifecycle.service import (
    revert_policy_to_draft as run_revert_policy_to_draft,
)
from ps_service.policy_lifecycle.service import (
    update_control_draft as run_update_control_draft,
)
from ps_service.policy_lifecycle.service import (
    update_policy_draft as run_update_policy_draft,
)
from ps_service.policy_lifecycle.service import (
    update_standard_draft as run_update_standard_draft,
)
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
from ps_service.runtime_config import (
    PsycopgRuntimeConfigStore,
    RuntimeConfigError,
    RuntimeConfigInvalidValueError,
    RuntimeConfigPersistenceError,
    RuntimeConfigUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from importlib.resources.abc import Traversable

    from ps_service.api.change_check_orchestration import ChangeCheckDependencies
    from ps_service.api.ingestion_orchestration import PipelineDependencies
    from ps_service.api.near_miss_review_orchestration import NearMissReviewDependencies
    from ps_service.api.restore_orchestration import CatalogRestoreDependencies
    from ps_service.audit.models import AuditEventRow
    from ps_service.authz.models import AccessRoleAssignmentRow
    from ps_service.config import ServiceConfig
    from ps_service.curated_source.catalog_client import CuratedCatalogDependencies
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
# mirrors every existing catalog short_name ("cra", "gdpr", "nis2"). Now imported as
# `SHORT_NAME_PATTERN` from `ps_service.api.ingestion_orchestration` (issue #146) --
# one canonical pattern literal shared with `api/models.py`'s
# `CatalogIngestionRequest.short_name`, aliased back to this module's original
# private name so `ingest_regulation`'s `Field(pattern=_SHORT_NAME_PATTERN)` call
# site needs no further edit.
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
    name="ps-mcp",
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


def _resolve_policy_lifecycle_actor(config: ServiceConfig) -> tuple[str, str] | None:
    """Resolve `(sub, iss)` for the policy-lifecycle tools' caller identity (issue #134, D-11).

    Same verified-bearer-token branch as `_resolve_signing_actor`/
    `_resolve_authz_actor` immediately above -- but, unlike either of them,
    DOES fall back to `(LOCAL_TEST_PRINCIPAL_ID, LOCAL_TEST_PRINCIPAL_ID)`
    when `config.is_local_test_bypass_active` and no verified bearer token
    is bound to this call at all, mirroring `_resolve_principal`'s own
    bypass-honoring branch instead (AC-BI-018). Unlike access-role
    management or signing-ceremony approval identity, a Policy draft
    created under the local-test bypass is exactly the kind of thing local,
    unauthenticated end-to-end testing needs to be able to do -- there is no
    real, permanent, identity-keyed store record this would corrupt the way
    `_resolve_authz_actor`'s own docstring warns about.

    The bypass fallback is reached ONLY when no bearer token is present at
    all (mirrors `_resolve_principal`'s own outer branching exactly) -- a
    *present but malformed* token (missing `sub`, or `iss` not a string)
    still resolves to `None`, never silently falling through to the bypass
    identity.

    Returns:
        The verified `(sub, iss)` pair; `(LOCAL_TEST_PRINCIPAL_ID,
        LOCAL_TEST_PRINCIPAL_ID)` if no verified bearer token is bound to
        this call AND the local-test bypass is active; else `None`.
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


_CLEANUP_REQUIRES_AUTHENTICATED_CALLER_MESSAGE = (
    "error: graph cleanup requires a real authenticated caller"
)


def _require_cleanup_actor(config: ServiceConfig) -> tuple[str, str] | str:
    """Gate every graph-cleanup tool (issue #190, AC-BI-001/002).

    Returns the verified `(sub, iss)` of a caller holding an explicit
    `ComplianceOfficer` grant, or an `error: ` string. Fail-closed by design:
    unlike `restore_instrument`, the local-test bypass does NOT skip the check --
    no verified bearer token means no actor means rejection (graph cleanup is a
    privileged, accountable edit and a synthetic identity must never hold it).
    `require_role` is exact-match for `ComplianceOfficer`, so SystemAdmin and
    SystemOwner get no override, and a store outage denies rather than allows.
    """
    actor = _resolve_authz_actor(config)
    if actor is None:
        return _CLEANUP_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
    try:
        require_role(
            actor,
            minimum=AccessRole.COMPLIANCE_OFFICER,
            store=PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config)),
        )
    except (AccessDeniedError, AuthorizationStoreUnavailableError) as exc:
        return f"error: {exc}"
    return actor


def _run_mcp_action(
    action: str,
    principal: str | None,
    body: Callable[[], dict[str, object] | str],
    *,
    entity_id: str | None = None,
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
        entity_id: The node id this call acts on (or, for a not-yet-created
            node, its parent), carried on every one of this call's three
            log entries (issue #136, CHANGES.md finding #9). Optional and
            defaults to `None` -- every pre-#136 caller omits it, so
            `LogEntry.entity_id` stays unset exactly as it did before this
            parameter existed; zero behavior change for any tool that
            doesn't pass it.

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
            entity_id=entity_id,
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
                entity_id=entity_id,
                extra={"principal": principal_extra, "detail": repr(exc)},
            )
            return _UNEXPECTED_ERROR_MESSAGE
        if isinstance(result, str) and result.startswith("error:"):
            emit_log_entry(
                component=_COMPONENT,
                action=action,
                outcome="failed",
                run_id=run_id,
                entity_id=entity_id,
                extra={"principal": principal_extra, "reason": result[:_STAGE_REASON_MAX_LEN]},
            )
            return result
        emit_log_entry(
            component=_COMPONENT,
            action=action,
            outcome="succeeded",
            run_id=run_id,
            entity_id=entity_id,
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


def _sanitize_cleanup_graph_open(
    dependencies: GraphCleanupDependencies,
) -> GraphCleanupDependencies:
    """Wrap the cleanup graph opener so any failure sanitises to `McpGraphUnavailableError`."""
    return dataclasses.replace(
        dependencies,
        open_single_tenant_graph=_sanitize_graph_open(dependencies.open_single_tenant_graph),
    )


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
    *,
    config: ServiceConfig,
    principal: str | None,
    run_id: str,
) -> dict[str, object] | str:
    """Resolve the request's identity, run the pipeline, and map its exceptions.

    Calls `resolve_ingestion_entry` (issue #193, AC-BI-014) -- the same shared
    function `routes.create_ingestion` calls -- against the single-tenant graph
    opened from this call's own sanitized dependency bundle. Identity is decided
    by the live graph and Cellar/ELI, never by the curated catalog: the function
    raises `CelexAlreadyIngestedError` when `celex` is already in the graph and
    `ShortNameCollisionError` when `short_name` is already claimed by a different
    CELEX; otherwise it resolves `celex` against Cellar/ELI under the normalized
    `short_name` (used verbatim, never derived from the fetched title, issue #96)
    and returns the entry plus the adapter bound to the fetched document. Any
    exception this function does not itself catch is the residual
    D-SANITIZE-UNEXPECTED row, left to `_run_mcp_action`'s own safety net.
    """
    try:
        dependencies = _sanitize_pipeline_graph_opens(build_default_pipeline_dependencies())
        single_tenant_graph = dependencies.graphs.single_tenant(config)
        resolution = resolve_ingestion_entry(
            celex, short_name, single_tenant_graph=single_tenant_graph
        )
        outcome = run_catalog_ingestion_pipeline(
            resolution.entry,
            config=config,
            run_id=run_id,
            caller=principal or "unknown",
            dependencies=dependencies,
            ingestion_adapter=resolution.adapter,
        )
    except (
        CatalogIdentifierNotFoundError,
        CelexAlreadyIngestedError,
        IngestionConfigIncompleteError,
        PipelineStageError,
        ShortNameCollisionError,
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
    a `source: "catalog"` request. `short_name` is always required and is
    normalized to upper case (so `cra` and `CRA` are the same name); the
    ingested instrument is `<SHORT_NAME>-<version>`. Any valid CELEX is
    resolved against Cellar/ELI and ingested under the `short_name` you give,
    used verbatim -- nothing is derived from the fetched title, so the same
    CELEX never forks into two differently-named graphs.

    On success, returns the same structured summary `POST /ingestions`
    returns: `run_id`, `regulatory_instrument_id`, `source`, and one
    `stages` entry per completed pipeline stage (ingestion, extraction,
    derivation, merge) with its own small integer `summary`. Returns a
    string beginning `error: ` when: `celex` is already ingested in the
    compliance graph (under any `short_name`); `short_name` is already
    claimed, in any letter case, by a different CELEX; `celex` does not
    exist on Cellar/ELI; the LLM Interface dependency is currently
    unhealthy (checked before any graph is opened or the pipeline is
    called); the service configuration is missing an LLM/embedding model
    or similarity threshold; the policy graph database cannot be reached;
    a pipeline stage genuinely fails mid-run; or (this tool's own residual
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
                    minimum=AccessRole.COMPLIANCE_OFFICER,
                    store=PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config)),
                )
            except (AccessDeniedError, AuthorizationStoreUnavailableError) as exc:
                return f"error: {exc}"
        if not dependency_health.is_healthy(dependency_health.LLM_INTERFACE):
            return _LLM_INTERFACE_UNAVAILABLE_MESSAGE
        # `_run_mcp_action` always binds a run_id via `bind_run_context()` before
        # calling this closure, so `current_run_id()` is never actually `None`
        # here -- the `""` fallback only satisfies the type checker's narrowing,
        # mirroring `_resolve_principal`'s own "unreachable in practice" idiom.
        run_id = current_run_id() or ""
        return _resolve_and_ingest(
            celex, short_name, config=config, principal=principal, run_id=run_id
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
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        if not config.is_local_test_bypass_active:
            if actor is None:
                return _CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
            try:
                require_role(
                    actor,
                    minimum=AccessRole.COMPLIANCE_OFFICER,
                    store=PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config)),
                )
            except (AccessDeniedError, AuthorizationStoreUnavailableError) as exc:
                return f"error: {exc}"
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


# issue #130: the actor `set-catalog-source`/`reset-catalog-source` record on their audit row when
# no real caller identity exists. `_resolve_authz_actor` deliberately returns `None` under the
# local-test bypass, and `audit_events` actors are NOT NULL, so a fixed sentinel (which can never
# collide with an IdP-issued `sub`, precedent `ps_service.authz.store`'s `system:bootstrap`)
# stands in.
_LOCAL_TEST_BYPASS_AUDIT_ACTOR = "system:local-test-bypass"

# Fixed, detail-free strings for the catalog-source tools' read failures (AC-BI-010): never
# host, port, driver text or the (possibly credential-bearing) override URL.
_RUNTIME_CONFIG_UNAVAILABLE_MESSAGE = (
    "error: The runtime configuration store is temporarily unavailable."
)


def _catalog_source_audit_actor(actor: tuple[str, str] | None) -> tuple[str, str]:
    """The `(subject, issuer)` a catalog-source write is audited as (issue #130)."""
    return actor or (_LOCAL_TEST_BYPASS_AUDIT_ACTOR, _LOCAL_TEST_BYPASS_AUDIT_ACTOR)


@server.tool(name="set-catalog-source")
def set_catalog_source(url: Annotated[str, Field(min_length=1)]) -> dict[str, object] | str:
    """SetCatalogSource: override the effective curated-content source (issue #125, AC-BI-012).

    Validates `url` against the exact same http(s)/TLS rules startup
    configuration uses (`ps_service.curated_source.source_url.
    validate_source_url` -- AC-BI-008/010): only `http(s)://` schemes are
    accepted, and a plain `http://` URL is rejected unless
    `PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP` is set for this process. On
    success, persists `url` in the PS state database as the effective
    curated-content source (issue #130) and writes one audit event for the
    change in the same transaction -- no restart required -- and it takes
    precedence over `PS_CURATEDSOURCE_URL`/the public default on every
    subsequent `GET /catalog` and artifact fetch (AC-BI-013), until
    `reset-catalog-source` is called.

    Since issue #133, requires the caller hold `SystemAdmin` or above
    (`ps_service.authz.service.require_role`) -- skipped entirely under the
    local-test bypass (PLAN.md §3.3), which never sees a real actor
    identity to check.

    On success, returns `{"url": <the validated url>, "source": "override"}`.
    Returns a string beginning `error: ` when the caller lacks the required
    access role, when `url` fails validation, when the runtime configuration
    store cannot be reached or the write could not be applied (nothing is
    changed), or (this tool's own residual safety net) on any other
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
            catalog_source_store.set_override(
                PsycopgRuntimeConfigStore(config, audit_store=PsycopgAuditStore(config)),
                url,
                actor=_catalog_source_audit_actor(actor),
            )
        except (
            RuntimeConfigInvalidValueError,
            RuntimeConfigUnavailableError,
            RuntimeConfigPersistenceError,
        ) as exc:
            # The validator's own message (unchanged from before #130) or a fixed store message.
            return f"error: {exc}"
        return {"url": url, "source": "override"}

    return _run_mcp_action("set_catalog_source", principal, _body)


@server.tool(name="reset-catalog-source")
def reset_catalog_source() -> dict[str, object] | str:
    """ResetCatalogSource: clear the persisted curated-content source override (AC-BI-014).

    Takes no parameters. On success, deletes the persisted override from the PS state
    database (a no-op if none was set) and writes one audit event in the same transaction --
    the effective source immediately reverts to
    `PS_CURATEDSOURCE_URL`/the public default on every subsequent
    `GET /catalog` and artifact fetch, no restart required.

    Since issue #133, requires the caller hold `SystemAdmin` or above
    (`ps_service.authz.service.require_role`) -- skipped entirely under the
    local-test bypass (PLAN.md §3.3), which never sees a real actor
    identity to check.

    On success, returns `{"url": <the env-var/default url>, "source": "default"}`.
    Returns a string beginning `error: ` when the caller lacks the required
    access role, when the runtime configuration store cannot be reached or the
    write could not be applied (nothing is changed), or (this tool's own
    residual safety net) on any other unexpected failure.
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
            catalog_source_store.reset_override(
                PsycopgRuntimeConfigStore(config, audit_store=PsycopgAuditStore(config)),
                actor=_catalog_source_audit_actor(actor),
            )
        except (RuntimeConfigUnavailableError, RuntimeConfigPersistenceError) as exc:
            return f"error: {exc}"
        return {"url": config.curated_source_base_url, "source": "default"}

    return _run_mcp_action("reset_catalog_source", principal, _body)


@server.tool(name="get-catalog-source")
def get_catalog_source() -> dict[str, object] | str:
    """GetCatalogSource: report the currently effective curated-content source (AC-BI-015).

    Takes no parameters. Checks for a persisted override in the PS state database first,
    falling back to `PS_CURATEDSOURCE_URL`/the public default only when the store answered
    and holds no override. Fails closed (issue #130): when the runtime configuration store
    cannot be read this tool returns an error and never reports the default source, since
    the operator's override may point elsewhere on purpose.

    Since issue #133, requires the caller hold `SystemAdmin` or above
    (`ps_service.authz.service.require_role`) -- skipped entirely under the
    local-test bypass (PLAN.md §3.3), which never sees a real actor
    identity to check.

    On success, returns `{"url": <the effective url>, "source": "override"}`
    when a persisted override is in effect, or `{"url": ..., "source":
    "default"}` otherwise. Returns a string beginning `error: ` when the
    caller lacks the required access role, when the override cannot be read,
    or (this tool's own residual safety net) on any other unexpected failure.
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
            effective = resolve_effective_source(
                config,
                store=PsycopgRuntimeConfigStore(config, audit_store=PsycopgAuditStore(config)),
            )
        except RuntimeConfigError:
            return _RUNTIME_CONFIG_UNAVAILABLE_MESSAGE
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
    curated-content source (a persisted override when one exists, else the
    configured env-var/default -- fail closed, issue #130: when the override
    cannot be read this tool returns an error rather than listing the
    default source), then fetches and parses `catalog.json` from it. Takes
    zero parameters -- there is no client-supplied input to validate, so
    AC-BI-005's format-validation surface does not apply here, a deliberate absence mirroring
    `check_regulations`/`near_misses_list`.

    On success, returns the same structured listing `GET /catalog` returns:
    an `instruments` list, one entry per curated instrument (external and
    internal, unfiltered), each carrying `instrument_id`, `title`,
    `source_type`, and `jurisdiction` (`None` for an internal-source entry).
    Returns a string beginning `error: ` when the override cannot be read,
    when the configured curated-content source is unreachable or returns a
    missing/malformed `catalog.json`, or (this tool's own residual safety
    net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        dependencies: CuratedCatalogDependencies = build_default_curated_catalog_dependencies()
        try:
            effective_source = dependencies.resolve_effective_source(config)
        except RuntimeConfigError:
            return _RUNTIME_CONFIG_UNAVAILABLE_MESSAGE
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
    (D-RESTORE-DELEGATE): resolves `instrument_id` case-insensitively against
    the curated catalog (issue #184, AC-BI-001 -- e.g. `cra-1.0` resolves to
    the catalog's own `CRA-1.0`), then fetches that canonical id's
    manifest/baseline/native artifact from the effective curated-content
    source (a persisted override when one exists, else the configured
    env-var/default), then restores it into the policy graph -- delegating
    directly to `run_restoration_from_catalog_source`, the exact same
    function the REST route calls, never reimplemented. `instrument_id` is
    validated against the same charset/length bound ps-cli's own `restore
    instrument` positional used, plus its explicit rejection of any `".."`
    substring (AC-BI-005, D-INSTRUMENT-ID-STRICTNESS) -- rejected at the MCP
    schema layer, before this tool's body ever runs.

    On success, returns the same structured summary ps-cli's `restore
    instrument` used to print: `instrument_id` and one `stages` entry per
    completed restore stage, each carrying its own `stage`/`status`
    (AC-BI-004). Returns a string beginning `error: ` when the configured
    curated-content source is unreachable or the fetched artifact is
    missing/malformed, when `instrument_id` matches more than one catalog
    entry case-insensitively (issue #184, AC-BI-003 -- the error names every
    colliding id and nothing is fetched), when `instrument_id` matches no
    catalog entry in any case (issue #184, AC-BI-004 -- the error says no
    catalog entry matches and names the closest candidate ids, and nothing
    is fetched), when the fetched artifact fails
    checksum/schema_version verification, when any other restore stage
    genuinely fails (including a missing `PS_COMPANYMERGE_SIMILARITY_
    THRESHOLD` configuration value), when the policy graph database cannot
    be reached, or (this tool's own residual safety net) on any other
    unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)
    # Issue #183: the restoring caller owns any imported draft Policy. Same resolution as the
    # policy-lifecycle tools (verified `(sub, iss)`, or the local-test bypass identity).
    owner = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if not config.is_local_test_bypass_active:
            if actor is None:
                return _CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
            try:
                require_role(
                    actor,
                    minimum=AccessRole.COMPLIANCE_OFFICER,
                    store=PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config)),
                )
            except (AccessDeniedError, AuthorizationStoreUnavailableError) as exc:
                return f"error: {exc}"
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
                owner=owner,
            )
        except (
            CatalogSourceOverrideUnavailableError,
            CuratedSourceUnavailableError,
            RestoreInstrumentIdAmbiguousError,
            RestoreInstrumentIdNotFoundError,
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
    access_role: Literal["SystemOwner", "SystemAdmin", "PolicyManager", "ComplianceOfficer"],
) -> dict[str, object] | str:
    """GrantAccessRole: grant `access_role` to `principal_subject` (issue #133).

    RBAC (PLAN.md §0.7, widened per CHANGES.md Appendix A; issue #145 adds
    `ComplianceOfficer`): granting `SystemAdmin` or `SystemOwner` requires
    the caller hold `SystemOwner`; granting `PolicyManager` or
    `ComplianceOfficer` requires the caller hold `SystemOwner` or
    `SystemAdmin`. A caller may never grant a role to themselves
    (AC-BI-005).

    On success, returns `{"principal_subject", "access_role",
    "granted_by_subject", "system_owner_floor_warning"}`. Returns a string
    beginning `error: ` when the caller has no real authenticated session
    (the local-test bypass included -- access-role management is never
    available under it), when `access_role` is not one of the four
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
    access_role: Literal["SystemOwner", "SystemAdmin", "PolicyManager", "ComplianceOfficer"],
) -> dict[str, object] | str:
    """RevokeAccessRole: revoke `access_role` from `principal_subject` (issue #133).

    RBAC (PLAN.md §0.9, widened per CHANGES.md Appendix A): revoking
    `SystemOwner` requires the caller hold `SystemOwner` **or**
    `SystemAdmin` -- once a second `SystemOwner` exists (via
    `grant-access-role`), a `SystemAdmin` can revoke one without the
    self-revoke block ever intervening. Revoking `SystemAdmin`/
    `PolicyManager`/`ComplianceOfficer` (issue #145 adds the latter) mirrors
    `grant-access-role`'s own actor requirement for each role. A caller may
    never revoke a role from themselves (AC-BI-005), checked before the
    `SystemOwner` floor check (AC-BI-006): revoking the last remaining
    active `SystemOwner` is rejected regardless of who the caller is.

    On success, returns `{"principal_subject", "access_role",
    "revoked_by_subject", "system_owner_floor_warning"}`. Returns a string
    beginning `error: ` when the caller has no real authenticated session
    (the local-test bypass included), when `access_role` is not one of the
    four roles this tool manages, when the caller lacks the required role,
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


_POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE = (
    "error: this action requires a real authenticated caller (the local-test bypass counts as one)"
)


class _MalformedPatchFieldsError(Exception):
    """A `fields` argument to one of issue #136's draft-content tools is shaped wrong.

    Raised only by `_parse_patch_fields` -- caught once, at each PATCH/add
    tool's own call site, and turned into an `error: ` string (mirrors
    `_MalformedPolicyDraftStandardsError`'s own convention immediately
    below).
    """


def _parse_patch_fields(
    fields: dict[str, object] | None,
    *,
    allowed: frozenset[str],
    enum_checks: Mapping[str, tuple[str, ...]] = MappingProxyType({}),
) -> dict[str, object]:
    """Validate `fields` against a tool's own patchable-field allow-list (issue #136, PLAN.md §1.7).

    `fields=None` (the parameter omitted entirely) returns `{}` (a no-op
    update) -- mirrors `_parse_policy_draft_standards`'s own `None`-means-
    nothing convention. Every key must be in `allowed`; every value must be
    `str | None` (`None` means "explicitly clear this field to null" --
    AC-BI-008's PATCH semantic, distinct from the key being absent, which
    leaves the existing value untouched). A key present in `enum_checks`
    must have a value from its own allowed tuple, when the value is not
    `None`.

    Args:
        fields: The tool's own raw `fields` argument, as the MCP caller
            supplied it.
        allowed: The fixed, code-defined set of patchable field names this
            tool accepts.
        enum_checks: Field name -> the tuple of values it is restricted to
            (checked only when that field is present and non-`None`).

    Returns:
        `fields` unchanged (as a plain `dict`), once every key/value has
        been validated.

    Raises:
        _MalformedPatchFieldsError: `fields` is not a dict, a key is not in
            `allowed`, a value is neither `str` nor `None`, or an
            `enum_checks`-restricted field's non-`None` value is not one of
            its own allowed values.
    """
    if fields is None:
        return {}
    for key, value in fields.items():
        if key not in allowed:
            raise _MalformedPatchFieldsError(f"fields.{key} is not a patchable field")
        if value is not None and not isinstance(value, str):
            raise _MalformedPatchFieldsError(f"fields.{key} must be a string or null")
        allowed_values = enum_checks.get(key)
        if allowed_values is not None and value is not None and value not in allowed_values:
            raise _MalformedPatchFieldsError(
                f"fields.{key} must be one of {allowed_values} or null"
            )
    return fields


_CREATE_POLICY_DRAFT_ERRORS = (
    PolicyCapabilityAlreadyGovernedError,
    PolicyCapabilityNotFoundError,
    PolicyNotFoundError,
    PolicySupersedePriorNotApprovedError,
    PolicyTitleAlreadyExistsError,
    PolicyLifecycleGraphUnavailableError,
)

_CREATE_POLICY_DRAFT_CONTROL_TYPES = ("automated", "manual")


class _MalformedPolicyDraftStandardsError(Exception):
    """The `create-policy-draft` `standards` argument is shaped wrong.

    Raised only by `_parse_policy_draft_standards`/its own helpers below --
    caught once, at `create_policy_draft`'s own call site, and turned into
    an `error: ` string (mirrors every other named-error tuple in this
    module) rather than ever crashing the tool call. The message always
    names the offending `standards[i]`/`standards[i].controls[j]` position
    so a caller can locate the bad entry in a multi-item list.
    """


def _require_standards_str_field(body: dict[str, object], field: str, *, where: str) -> str:
    """Return `body[field]` if it is a non-empty string; raise otherwise.

    Mirrors `curated_source.catalog_client._require_str`'s own convention.
    """
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise _MalformedPolicyDraftStandardsError(f"{where}.{field} must be a non-empty string")
    return value


def _parse_control_draft_input(raw: object, *, where: str) -> ControlDraftInput:
    """Parse one `standards[i].controls[j]` entry into a `ControlDraftInput`."""
    if not isinstance(raw, dict):
        raise _MalformedPolicyDraftStandardsError(f"{where} must be an object")
    body = cast("dict[str, object]", raw)
    title = _require_standards_str_field(body, "title", where=where)
    control_type = body.get("control_type", "manual")
    if control_type not in _CREATE_POLICY_DRAFT_CONTROL_TYPES:
        raise _MalformedPolicyDraftStandardsError(
            f"{where}.control_type must be 'automated' or 'manual'"
        )
    return ControlDraftInput(title=title, control_type=control_type)


def _parse_standard_draft_input(raw: object, *, where: str) -> StandardDraftInput:
    """Parse one `standards[i]` entry (with its own optional `controls`).

    Each `controls[j]` entry is parsed by `_parse_control_draft_input` in
    turn.
    """
    if not isinstance(raw, dict):
        raise _MalformedPolicyDraftStandardsError(f"{where} must be an object")
    body = cast("dict[str, object]", raw)
    title = _require_standards_str_field(body, "title", where=where)
    raw_controls = body.get("controls", [])
    if not isinstance(raw_controls, list):
        raise _MalformedPolicyDraftStandardsError(f"{where}.controls must be a list")
    controls = tuple(
        _parse_control_draft_input(item, where=f"{where}.controls[{index}]")
        for index, item in enumerate(cast("list[object]", raw_controls))
    )
    return StandardDraftInput(title=title, controls=controls)


def _parse_policy_draft_standards(
    standards: list[dict[str, object]] | None,
) -> tuple[StandardDraftInput, ...]:
    """Parse `create-policy-draft`'s optional `standards` argument top to bottom.

    `None` (the parameter omitted entirely) means "no Standards" -- the
    exact same zero-Standard draft `create-policy-draft` always produced
    before this argument existed (backward-compatible).
    """
    if standards is None:
        return ()
    return tuple(
        _parse_standard_draft_input(item, where=f"standards[{index}]")
        for index, item in enumerate(standards)
    )


@server.tool(name="create-policy-draft")
def create_policy_draft(
    title: Annotated[str, Field(min_length=1)],
    standards: list[dict[str, object]] | None = None,
    supersedes_policy_id: Annotated[str, Field(min_length=1)] | None = None,
    capability_ids: list[Annotated[str, Field(min_length=1)]] | None = None,
) -> dict[str, object] | str:
    """CreatePolicyDraft: mint a new draft Policy owned by the calling caller (issue #134/#136).

    Delegates to `ps_service.policy_lifecycle.service.create_policy_draft`
    (L2 "delegate, don't reimplement") -- this tool resolves the caller's
    identity and the policy graph handle, parses `standards`, then does
    nothing else. The new Policy is minted with `status="draft"`, owned by
    the calling caller.

    `standards` is optional (omit it, or pass `null`, for a title-only,
    zero-Standard draft -- fully backward compatible). When given, it is a
    list of objects, each `{"title": <str>, "controls": [...]}` (`controls`
    itself optional, defaulting to `[]`); each control is `{"title": <str>,
    "control_type": "automated" | "manual"}` (`control_type` optional,
    defaulting to `"manual"`). Every Standard/Control minted this way is
    unconditionally `status="draft"` (D-6), regardless of anything else in
    the request.

    `supersedes_policy_id` (issue #136, the amendment fork) is optional. When
    omitted, this mints an ordinary v1 Policy: its id is derived
    deterministically from `title` alone (`pol_{slug}_{hash}`), so calling
    this again with the same title returns an error rather than a second
    Policy, and `version` is `"1"`. When set to an existing `"approved"`
    Policy's id, this instead mints a SUCCESSOR draft: the new id is derived
    from BOTH `title` and `supersedes_policy_id`, `version` is
    `str(int(prior_version) + 1)` (still string-typed), a single Policy-
    level `SUPERSEDED_BY` edge links the prior Policy to the new one (no
    per-Standard/Control lineage edges), and the new draft's Standard/
    Control children are the prior Policy's OWN CURRENT children, forked as
    brand-new, independently-editable nodes -- never the prior's own nodes,
    which are never mutated by this call. **`standards` is silently ignored
    when `supersedes_policy_id` is set** -- the fork's content always comes
    from the prior tree, never a caller-supplied list. `supersedes_policy_id`
    naming a Policy that does not exist, or that exists but is not currently
    `"approved"`, is rejected with a named error; no fork is attempted.

    `capability_ids` (issue #185) is optional. On a FRESH draft (no
    `supersedes_policy_id`) it names existing Capabilities the new Policy
    will govern: the `GOVERNED_BY` edges are written together with the Policy
    node, in one guarded step, so the Policy governs those Capabilities from
    creation (while still a draft) until it is approved or removed. Duplicates
    are dropped (order kept). Every id must name an existing Capability that
    no Policy governs yet -- otherwise a named error is returned and nothing
    is written. **`capability_ids` is silently ignored when
    `supersedes_policy_id` is set**: a fork's Capabilities stay on the prior
    Policy and move to the successor when it is approved.

    On success, returns `{"policy_id", "title", "status", "version",
    "owner_subject", "standard_ids", "control_ids", "superseded_policy_id",
    "governed_capability_ids"}` -- `governed_capability_ids` are the
    Capabilities now governed by the draft (`[]` when none, or on a fork);
    `standard_ids`/`control_ids` are `[]` on a title-only draft, or every
    minted/forked child's id otherwise, immediately usable in a following
    `update-standard-draft`/`add-control-to-draft`/`update-control-draft`
    call; `superseded_policy_id` is `null` unless this was a fork. Returns a
    string beginning `error: ` when the caller has no real authenticated
    session (the local-test bypass DOES count as one here -- unlike
    access-role management or signing-ceremony approval), when `standards`
    (or a nested `controls` entry) is shaped wrong (not a list of objects, or
    missing/empty a required `title`, or an invalid `control_type`), when
    `supersedes_policy_id` names a Policy that does not exist or is not
    `"approved"`, when a `capability_ids` entry names no Capability or one
    that is already governed, when the computed id already collides with an
    existing Policy, when the policy graph cannot be reached, or (this tool's own
    residual safety net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            parsed_standards = _parse_policy_draft_standards(standards)
        except _MalformedPolicyDraftStandardsError as exc:
            return f"error: {exc}"
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        audit_store = PsycopgAuditStore(config)
        try:
            result = run_create_policy_draft(
                actor=actor,
                title=title,
                standards=parsed_standards,
                supersedes_policy_id=supersedes_policy_id,
                capability_ids=tuple(capability_ids or ()),
                graph=graph,
                audit_store=audit_store,
            )
        except _CREATE_POLICY_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
            "version": result.version,
            "owner_subject": result.owner_subject,
            "standard_ids": list(result.standard_ids),
            "control_ids": list(result.control_ids),
            "superseded_policy_id": result.superseded_policy_id,
            "governed_capability_ids": list(result.capability_ids),
        }

    return _run_mcp_action("create_policy_draft", principal, _body, entity_id=supersedes_policy_id)


_UPDATE_POLICY_DRAFT_ERRORS = (
    PolicyNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@server.tool(name="update-policy-draft")
def update_policy_draft(
    policy_id: Annotated[str, Field(min_length=1)],
    fields: dict[str, object] | None = None,
) -> dict[str, object] | str:
    """UpdatePolicyDraft: PATCH a subset of a draft Policy's own content fields (issue #136).

    Delegates to `ps_service.policy_lifecycle.service.update_policy_draft`
    (L2 "delegate, don't reimplement") -- this tool resolves the caller's
    identity, the policy graph handle, and the access-role store, parses
    `fields`, then does nothing else. Uses the exact same
    `_resolve_policy_lifecycle_actor` identity resolution as
    `create-policy-draft`/`get-policy` (D-11): the local-test bypass DOES
    count as a real authenticated caller here.

    Only usable while `policy_id`'s own governance status is `"draft"`, and
    only by its owner or a caller holding `SystemOwner`/`SystemAdmin`
    (AC-BI-002/004) -- the same visibility/override rule `get-policy`
    already applies to a Draft Policy.

    `fields` is a partial-update map: only the keys supplied are changed,
    every omitted field keeps its existing value (AC-BI-008). Each value is
    either a non-null string, or JSON `null` to explicitly clear that field.
    Allowed keys: `description`, `scope_in`, `scope_out`,
    `normative_commitments`, `review_cadence`, `exception_pathway`,
    `measurable_outcomes`, `capability_grouping_rationale`. `title`,
    `status`, `owner_subject`, `owner_issuer`, and `version` can never be
    patched through this tool.

    On success, returns `{"policy_id", "updated_fields"}` (`updated_fields`
    is every key `fields` supplied, sorted). Returns a string beginning
    `error: ` when the caller has no real authenticated session, when
    `fields` names an unknown key or a non-string/non-null value, when no
    Policy exists with `policy_id`, when the caller is neither the owner nor
    a `SystemOwner`/`SystemAdmin`, when `policy_id`'s Policy is not currently
    `"draft"`, when the policy graph cannot be reached, or (this tool's own
    residual safety net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            parsed_fields = _parse_patch_fields(fields, allowed=_POLICY_PATCHABLE_FIELDS)
        except _MalformedPatchFieldsError as exc:
            return f"error: {exc}"
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        access_role_store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
        try:
            result = run_update_policy_draft(
                actor=actor,
                policy_id=policy_id,
                fields=parsed_fields,
                graph=graph,
                access_role_store=access_role_store,
            )
        except _UPDATE_POLICY_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "updated_fields": list(result.updated_fields),
        }

    return _run_mcp_action("update_policy_draft", principal, _body, entity_id=policy_id)


_ADD_STANDARD_TO_DRAFT_ERRORS = (
    PolicyNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@server.tool(name="add-standard-to-draft")
def add_standard_to_draft(
    policy_id: Annotated[str, Field(min_length=1)],
    title: Annotated[str, Field(min_length=1)],
    fields: dict[str, object] | None = None,
) -> dict[str, object] | str:
    """AddStandardToDraft: create a new Standard under a draft Policy (issue #136).

    Delegates to `ps_service.policy_lifecycle.service.add_standard_to_draft`
    (L2 "delegate, don't reimplement") -- this tool resolves the caller's
    identity, the policy graph handle, and the access-role store, parses
    `fields`, then does nothing else. Uses the exact same
    `_resolve_policy_lifecycle_actor` identity resolution as
    `create-policy-draft`/`update-policy-draft` (D-11): the local-test
    bypass DOES count as a real authenticated caller here.

    Only usable while `policy_id`'s own governance status is `"draft"`, and
    only by its owner or a caller holding `SystemOwner`/`SystemAdmin`
    (AC-BI-003/004) -- the same visibility/override rule `update-policy-draft`
    already applies to the parent Policy. The new Standard is linked to
    `policy_id` via `SUPPORTED_BY`, and is always minted with governance
    status `"draft"`, independent of its own `implementation_status` field
    (AC-BI-007) -- `implementation_status` defaults to `"draft"` unless
    supplied in `fields`.

    `fields` is optional extra content to set at creation time (same partial-
    update value shape as `update-policy-draft`'s own `fields`): each value
    is either a non-null string, or JSON `null` (meaning "leave this field
    unset" -- a newly-minted node has no prior value to clear, unlike a PATCH
    tool). Allowed keys: `description`, `implementation_status`, `procedure`,
    `implementer_role`, `reviewer_role`, `applicability_boundary`,
    `verification_notes`, `change_rationale`. `implementation_status`, when
    supplied, must be one of `draft`, `implemented`, `reviewed`, `deprecated`.
    `title` and `status` can never be set through `fields`.

    On success, returns `{"standard_id", "policy_id", "title", "status"}` --
    `standard_id` is usable immediately in a following call (AC-BI-009).
    Returns a string beginning `error: ` when the caller has no real
    authenticated session, when `fields` names an unknown key, a non-string/
    non-null value, or an invalid `implementation_status`, when no Policy
    exists with `policy_id`, when the caller is neither the owner nor a
    `SystemOwner`/`SystemAdmin`, when `policy_id`'s Policy is not currently
    `"draft"`, when the policy graph cannot be reached, or (this tool's own
    residual safety net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            parsed_fields = _parse_patch_fields(
                fields,
                allowed=_STANDARD_PATCHABLE_FIELDS,
                enum_checks={"implementation_status": _STANDARD_IMPLEMENTATION_STATUS_VALUES},
            )
        except _MalformedPatchFieldsError as exc:
            return f"error: {exc}"
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        access_role_store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
        try:
            result = run_add_standard_to_draft(
                actor=actor,
                policy_id=policy_id,
                title=title,
                fields=parsed_fields,
                graph=graph,
                access_role_store=access_role_store,
            )
        except _ADD_STANDARD_TO_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "standard_id": result.standard_id,
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
        }

    return _run_mcp_action("add_standard_to_draft", principal, _body, entity_id=policy_id)


_UPDATE_STANDARD_DRAFT_ERRORS = (
    PolicyStandardNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@server.tool(name="update-standard-draft")
def update_standard_draft(
    standard_id: Annotated[str, Field(min_length=1)],
    fields: dict[str, object] | None = None,
) -> dict[str, object] | str:
    """UpdateStandardDraft: PATCH a subset of a draft Standard's own content fields (issue #136).

    Delegates to `ps_service.policy_lifecycle.service.update_standard_draft`
    (L2 "delegate, don't reimplement") -- this tool resolves the caller's
    identity, the policy graph handle, and the access-role store, parses
    `fields`, then does nothing else. Uses the exact same
    `_resolve_policy_lifecycle_actor` identity resolution as
    `update-policy-draft`/`add-standard-to-draft` (D-11): the local-test
    bypass DOES count as a real authenticated caller here.

    Ownership is derived TRANSITIVELY from `standard_id`'s parent Policy
    (AC-BI-003) -- a Standard has no ownership field of its own; only its
    owner or a caller holding `SystemOwner`/`SystemAdmin` may patch it, the
    same visibility/override rule every other draft-content tool in this
    issue applies. Only usable while `standard_id`'s own governance status
    (not the parent Policy's) is currently `"draft"` (AC-BI-004).

    `fields` is a partial-update map: only the keys supplied are changed,
    every omitted field keeps its existing value (AC-BI-008). Each value is
    either a non-null string, or JSON `null` to explicitly clear that field.
    Allowed keys: `description`, `implementation_status`, `procedure`,
    `implementer_role`, `reviewer_role`, `applicability_boundary`,
    `verification_notes`, `change_rationale`. `implementation_status`, when
    supplied, must be one of `draft`, `implemented`, `reviewed`, `deprecated`.
    `title` and `status` can never be patched through this tool.

    On success, returns `{"standard_id", "policy_id", "title", "status"}`.
    Returns a string beginning `error: ` when the caller has no real
    authenticated session, when `fields` names an unknown key, a non-string/
    non-null value, or an invalid `implementation_status`, when no Standard
    exists with `standard_id`, when the caller is neither the parent Policy's
    owner nor a `SystemOwner`/`SystemAdmin`, when `standard_id`'s Standard is
    not currently `"draft"`, when the policy graph cannot be reached, or
    (this tool's own residual safety net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            parsed_fields = _parse_patch_fields(
                fields,
                allowed=_STANDARD_PATCHABLE_FIELDS,
                enum_checks={"implementation_status": _STANDARD_IMPLEMENTATION_STATUS_VALUES},
            )
        except _MalformedPatchFieldsError as exc:
            return f"error: {exc}"
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        access_role_store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
        try:
            result = run_update_standard_draft(
                actor=actor,
                standard_id=standard_id,
                fields=parsed_fields,
                graph=graph,
                access_role_store=access_role_store,
            )
        except _UPDATE_STANDARD_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "standard_id": result.standard_id,
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
        }

    return _run_mcp_action("update_standard_draft", principal, _body, entity_id=standard_id)


_ADD_CONTROL_TO_DRAFT_ERRORS = (
    PolicyStandardNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)

# CHANGES.md finding #8: `add-control-to-draft` excludes `"type"` from its
# own `fields` allow-list -- the top-level `control_type` param is the only
# way to set a Control's type at creation. `update-control-draft` (Slice 5)
# keeps the full, unmodified `_CONTROL_PATCHABLE_FIELDS` (including `"type"`)
# as the only post-creation path to change it.
_ADD_CONTROL_TO_DRAFT_PATCHABLE_FIELDS = _CONTROL_PATCHABLE_FIELDS - {"type"}


@server.tool(name="add-control-to-draft")
def add_control_to_draft(
    standard_id: Annotated[str, Field(min_length=1)],
    title: Annotated[str, Field(min_length=1)],
    control_type: Annotated[str, Field(min_length=1)] = "manual",
    fields: dict[str, object] | None = None,
) -> dict[str, object] | str:
    """AddControlToDraft: create a new Control under a draft Standard (issue #136).

    Delegates to `ps_service.policy_lifecycle.service.add_control_to_draft`
    (L2 "delegate, don't reimplement") -- this tool resolves the caller's
    identity, the policy graph handle, and the access-role store, validates
    `control_type`, parses `fields`, then does nothing else. Uses the exact
    same `_resolve_policy_lifecycle_actor` identity resolution as every other
    tool in this issue (D-11): the local-test bypass DOES count as a real
    authenticated caller here.

    Ownership is derived from `standard_id`'s parent Policy via the SAME
    one-hop traversal `update-standard-draft` uses (PLAN.md §1.4 -- both
    tools take a `standard_id`; only `update-control-draft`, keyed on an
    existing `control_id`, needs a genuine two-hop traversal) -- only usable
    while `standard_id`'s own governance status (not the parent Policy's) is
    currently `"draft"` (AC-BI-004), and only by the parent Policy's owner or
    a caller holding `SystemOwner`/`SystemAdmin` (AC-BI-003). The new Control
    is linked to `standard_id` via `IMPLEMENTED_BY`, and is always minted
    with governance status `"draft"`, independent of its own
    `implementation_status` field (AC-BI-007) -- `implementation_status`
    defaults to `"planned"` (NOT `"draft"` -- Control's own workflow starts
    one step later than Standard's) unless supplied in `fields`.

    `control_type` must be `"automated"` or `"manual"`. `fields` is optional
    extra content to set at creation time (same partial-update value shape as
    `add-standard-to-draft`'s own `fields`): each value is either a non-null
    string, or JSON `null` (meaning "leave this field unset" -- a newly-
    minted node has no prior value to clear). Allowed keys: `description`,
    `implementation_status`, `execution_frequency`, `last_test_date`,
    `next_review_date`, `evidence_ref`, `pass_fail_criteria`,
    `execution_method`, `evidence_plan`, `executor_role`, `reviewer_role`,
    `risk_alignment_rationale`. `implementation_status`, when supplied, must
    be one of `planned`, `implemented`, `reviewed`, `deprecated`. `title`,
    `status`, and `type` can never be set through `fields` -- `fields.type`
    is rejected; use `control_type` to set the Control's type at creation.

    On success, returns `{"control_id", "standard_id", "policy_id", "title",
    "status"}` -- `control_id` is usable immediately in a following call.
    Returns a string beginning `error: ` when the caller has no real
    authenticated session, when `control_type` is not `"automated"` or
    `"manual"`, when `fields` names an unknown key (including `"type"`), a
    non-string/non-null value, or an invalid `implementation_status`, when no
    Standard exists with `standard_id`, when the caller is neither the parent
    Policy's owner nor a `SystemOwner`/`SystemAdmin`, when `standard_id`'s
    Standard is not currently `"draft"`, when the policy graph cannot be
    reached, or (this tool's own residual safety net) on any other
    unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        if control_type not in _CREATE_POLICY_DRAFT_CONTROL_TYPES:
            return "error: control_type must be 'automated' or 'manual'"
        try:
            parsed_fields = _parse_patch_fields(
                fields,
                allowed=_ADD_CONTROL_TO_DRAFT_PATCHABLE_FIELDS,
                enum_checks={"implementation_status": _CONTROL_IMPLEMENTATION_STATUS_VALUES},
            )
        except _MalformedPatchFieldsError as exc:
            return f"error: {exc}"
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        access_role_store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
        try:
            result = run_add_control_to_draft(
                actor=actor,
                standard_id=standard_id,
                title=title,
                control_type=control_type,
                fields=parsed_fields,
                graph=graph,
                access_role_store=access_role_store,
            )
        except _ADD_CONTROL_TO_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "control_id": result.control_id,
            "standard_id": result.standard_id,
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
        }

    return _run_mcp_action("add_control_to_draft", principal, _body, entity_id=standard_id)


_UPDATE_CONTROL_DRAFT_ERRORS = (
    PolicyControlNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@server.tool(name="update-control-draft")
def update_control_draft(
    control_id: Annotated[str, Field(min_length=1)],
    fields: dict[str, object] | None = None,
) -> dict[str, object] | str:
    """UpdateControlDraft: PATCH a subset of a draft Control's own content fields (issue #136).

    Delegates to `ps_service.policy_lifecycle.service.update_control_draft`
    (L2 "delegate, don't reimplement") -- this tool resolves the caller's
    identity, the policy graph handle, and the access-role store, parses
    `fields`, then does nothing else. Uses the exact same
    `_resolve_policy_lifecycle_actor` identity resolution as every other tool
    in this issue (D-11): the local-test bypass DOES count as a real
    authenticated caller here.

    Ownership is derived TRANSITIVELY from `control_id`'s root Policy, via
    the genuinely TWO-hop `Policy -[:SUPPORTED_BY]-> Standard
    -[:IMPLEMENTED_BY]-> Control` traversal (AC-BI-003) -- neither Control
    nor its parent Standard has an ownership field of its own; unlike
    `update-standard-draft`/`add-control-to-draft`, which both take a
    `standard_id` and only need a one-hop traversal to their parent Policy.
    Only usable while `control_id`'s own governance status (not its parent
    Standard's or root Policy's) is currently `"draft"` (AC-BI-004).

    `fields` is a partial-update map: only the keys supplied are changed,
    every omitted field keeps its existing value (AC-BI-008). Each value is
    either a non-null string, or JSON `null` to explicitly clear that field.
    Allowed keys: `description`, `implementation_status`, `type`,
    `execution_frequency`, `last_test_date`, `next_review_date`,
    `evidence_ref`, `pass_fail_criteria`, `execution_method`,
    `evidence_plan`, `executor_role`, `reviewer_role`,
    `risk_alignment_rationale` -- unlike `add-control-to-draft`, `type` IS
    patchable here: this is the only post-creation path to change a
    Control's type (CHANGES.md finding #8). `implementation_status`, when
    supplied, must be one of `planned`, `implemented`, `reviewed`,
    `deprecated`. `title` and `status` can never be patched through this
    tool.

    On success, returns `{"control_id", "standard_id", "policy_id", "title",
    "status"}`. Returns a string beginning `error: ` when the caller has no
    real authenticated session, when `fields` names an unknown key, a
    non-string/non-null value, or an invalid `implementation_status`, when no
    Control exists with `control_id`, when the caller is neither the root
    Policy's owner nor a `SystemOwner`/`SystemAdmin`, when `control_id`'s
    Control is not currently `"draft"`, when the policy graph cannot be
    reached, or (this tool's own residual safety net) on any other
    unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            parsed_fields = _parse_patch_fields(
                fields,
                allowed=_CONTROL_PATCHABLE_FIELDS,
                enum_checks={"implementation_status": _CONTROL_IMPLEMENTATION_STATUS_VALUES},
            )
        except _MalformedPatchFieldsError as exc:
            return f"error: {exc}"
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        access_role_store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
        try:
            result = run_update_control_draft(
                actor=actor,
                control_id=control_id,
                fields=parsed_fields,
                graph=graph,
                access_role_store=access_role_store,
            )
        except _UPDATE_CONTROL_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "control_id": result.control_id,
            "standard_id": result.standard_id,
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
        }

    return _run_mcp_action("update_control_draft", principal, _body, entity_id=control_id)


_GET_POLICY_ERRORS = (PolicyNotFoundError, PolicyDraftAccessDeniedError)


@server.tool(name="get-policy")
def get_policy(policy_id: Annotated[str, Field(min_length=1)]) -> dict[str, object] | str:
    """GetPolicy: read one Policy plus its full Standard/Control tree (issue #134, S14).

    Delegates to `ps_service.policy_lifecycle.service.get_policy` (L2
    "delegate, don't reimplement") -- this tool resolves the caller's
    identity, the policy graph handle, and the access-role store, then does
    nothing else. Uses the exact same `_resolve_policy_lifecycle_actor`
    identity resolution as `create-policy-draft` (D-11): the local-test
    bypass DOES count as a real authenticated caller here.

    A Draft Policy is visible only to its own owner or to a caller holding
    `SystemOwner`/`SystemAdmin` (AC-BI-002) -- a non-owner `PolicyManager` is
    rejected the same as any other non-owner, non-elevated caller. A
    Proposed/Approved/Deprecated Policy is visible to any authenticated
    caller, no further check.

    On success, returns `{"policy_id", "title", "status", "version",
    "owner_subject", "owner_issuer", "standards"}`, where `standards` is a
    list of `{"standard_id", "title", "status", "controls"}` (each
    `controls` a list of `{"control_id", "title", "control_type",
    "status"}`). Returns a string beginning `error: ` when the caller has no
    real authenticated session, when no Policy exists with `policy_id`, when
    the caller lacks visibility into a Draft Policy it does not own, when
    the policy graph cannot be reached, or (this tool's own residual safety
    net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        access_role_store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
        try:
            result = run_get_policy(
                actor=actor,
                policy_id=policy_id,
                graph=graph,
                access_role_store=access_role_store,
            )
        except _GET_POLICY_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
            "version": result.version,
            "owner_subject": result.owner_subject,
            "owner_issuer": result.owner_issuer,
            "standards": [
                {
                    "standard_id": standard.standard_id,
                    "title": standard.title,
                    "status": standard.status,
                    "controls": [
                        {
                            "control_id": control.control_id,
                            "title": control.title,
                            "control_type": control.control_type,
                            "status": control.status,
                        }
                        for control in standard.controls
                    ],
                }
                for standard in result.standards
            ],
        }

    return _run_mcp_action("get_policy", principal, _body)


_PROPOSE_POLICY_ERRORS = (
    PolicyNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyIncompleteForProposalError,
    PolicyLifecycleGraphUnavailableError,
)


@server.tool(name="propose-policy")
def propose_policy(policy_id: Annotated[str, Field(min_length=1)]) -> dict[str, object] | str:
    """ProposePolicy: move a Draft Policy (and its whole tree) to Proposed (issue #134, S16).

    Delegates to `ps_service.policy_lifecycle.service.propose_policy` (L2
    "delegate, don't reimplement") -- this tool resolves the caller's
    identity and the policy graph handle, then does nothing else. Uses the
    exact same `_resolve_policy_lifecycle_actor` identity resolution as
    `create-policy-draft`/`get-policy` (D-11): the local-test bypass DOES
    count as a real authenticated caller here.

    Only the Policy's own owner may propose it, and only while it is still
    `"draft"` with at least one Standard attached -- on success, the Policy
    and every Standard/Control in its tree move to `"proposed"` in one
    cascading graph write.

    On success, returns `{"policy_id", "status", "standard_ids",
    "control_ids"}`. Returns a string beginning `error: ` when the caller
    has no real authenticated session, when no Policy exists with
    `policy_id`, when the caller does not own it, when it is not currently
    `"draft"`, when it has zero Standards, when the policy graph cannot be
    reached, or (this tool's own residual safety net) on any other
    unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        audit_store = PsycopgAuditStore(config)
        try:
            result = run_propose_policy(
                actor=actor, policy_id=policy_id, graph=graph, audit_store=audit_store
            )
        except _PROPOSE_POLICY_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "status": result.status,
            "standard_ids": list(result.standard_ids),
            "control_ids": list(result.control_ids),
        }

    return _run_mcp_action("propose_policy", principal, _body)


_APPROVE_POLICY_ERRORS = (
    PolicyNotFoundError,
    AccessDeniedError,
    PolicySelfApprovalBlockedError,
    PolicyInvalidStatusTransitionError,
    PolicyGovernanceConflictError,
    PolicyLifecycleGraphUnavailableError,
    AuthorizationStoreUnavailableError,
)


@server.tool(name="approve-policy")
def approve_policy(policy_id: Annotated[str, Field(min_length=1)]) -> dict[str, object] | str:
    """ApprovePolicy: move a Proposed Policy (and its whole tree) to Approved (issue #134, S18).

    Delegates to `ps_service.policy_lifecycle.service.approve_policy` (L2
    "delegate, don't reimplement") -- this tool resolves the caller's
    identity, the policy graph handle, and the access-role store, then does
    nothing else. Uses the exact same `_resolve_policy_lifecycle_actor`
    identity resolution as `create-policy-draft`/`get-policy`/
    `propose-policy` (D-11): the local-test bypass DOES count as a real
    authenticated caller here.

    Requires the caller hold `PolicyManager` (`ps_service.authz.service.
    require_role`) and never the Policy's own owner (self-approval is always
    blocked, even for a `PolicyManager` who also happens to own it) -- only
    while the Policy is currently `"proposed"`. On success, the Policy and
    every Standard/Control in its tree move to `"approved"` in one cascading
    graph write; if the Policy is a fork (`create-policy-draft` with
    `supersedes_policy_id`) of an approved prior, that prior's whole tree is
    automatically cascaded to `"deprecated"` in the same call, as its own
    separate audit event. When that prior governs Capabilities, their
    `GOVERNED_BY` edges move to the approved Policy in the same single write
    as the status change (all-or-nothing); a fork whose prior governs none
    approves without moving edges.

    On success, returns `{"policy_id", "status", "standard_ids",
    "control_ids", "auto_deprecated_policy_id", "governed_capability_ids"}`
    -- `auto_deprecated_policy_id` is `None` unless an auto-deprecation
    cascade also ran; `governed_capability_ids` lists the Capabilities whose
    governance moved (empty when none). Returns a string beginning
    `error: ` when the caller has no real authenticated session, when no
    Policy exists with `policy_id`, when the caller does not hold
    `PolicyManager`, when the caller is the Policy's own owner, when it is
    not currently `"proposed"`, when the Capabilities governed by the
    superseded Policy changed during approval (nothing was changed; retry),
    when the policy graph or the authorization store cannot be reached, or
    (this tool's own residual safety net) on any
    other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        audit_store = PsycopgAuditStore(config)
        access_role_store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
        try:
            result = run_approve_policy(
                actor=actor,
                policy_id=policy_id,
                graph=graph,
                audit_store=audit_store,
                access_role_store=access_role_store,
            )
        except _APPROVE_POLICY_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "status": result.status,
            "standard_ids": list(result.standard_ids),
            "control_ids": list(result.control_ids),
            "auto_deprecated_policy_id": result.auto_deprecated_policy_id,
            "governed_capability_ids": list(result.governed_capability_ids),
        }

    return _run_mcp_action("approve_policy", principal, _body)


_REJECT_POLICY_ERRORS = (
    PolicyNotFoundError,
    AccessDeniedError,
    PolicySelfApprovalBlockedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
    AuthorizationStoreUnavailableError,
)


@server.tool(name="reject-policy")
def reject_policy(policy_id: Annotated[str, Field(min_length=1)]) -> dict[str, object] | str:
    """RejectPolicy: move a Proposed Policy (and its whole tree) back to Draft (issue #134, S20).

    Delegates to `ps_service.policy_lifecycle.service.reject_policy` (L2
    "delegate, don't reimplement") -- this tool resolves the caller's
    identity, the policy graph handle, and the access-role store, then does
    nothing else. Mirrors `approve-policy`'s exact wrapper pattern (D-11):
    the local-test bypass DOES count as a real authenticated caller here.

    Requires the caller hold `PolicyManager` (`ps_service.authz.service.
    require_role`) and never the Policy's own owner (self-rejection is
    always blocked, even for a `PolicyManager` who also happens to own it)
    -- only while the Policy is currently `"proposed"`. On success, the
    Policy and every Standard/Control in its tree move back to `"draft"` in
    one cascading graph write (AC-BI-005). Unlike `approve-policy`, no
    auto-deprecation cascade ever runs here (D-10 is approve-only).

    On success, returns `{"policy_id", "status", "standard_ids",
    "control_ids"}`. Returns a string beginning `error: ` when the caller
    has no real authenticated session, when no Policy exists with
    `policy_id`, when the caller does not hold `PolicyManager`, when the
    caller is the Policy's own owner, when it is not currently `"proposed"`,
    when the policy graph or the authorization store cannot be reached, or
    (this tool's own residual safety net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        audit_store = PsycopgAuditStore(config)
        access_role_store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
        try:
            result = run_reject_policy(
                actor=actor,
                policy_id=policy_id,
                graph=graph,
                audit_store=audit_store,
                access_role_store=access_role_store,
            )
        except _REJECT_POLICY_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "status": result.status,
            "standard_ids": list(result.standard_ids),
            "control_ids": list(result.control_ids),
        }

    return _run_mcp_action("reject_policy", principal, _body)


_REVERT_POLICY_TO_DRAFT_ERRORS = (
    PolicyNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@server.tool(name="revert-policy-to-draft")
def revert_policy_to_draft(
    policy_id: Annotated[str, Field(min_length=1)],
) -> dict[str, object] | str:
    """RevertPolicyToDraft: move a Proposed Policy (and its whole tree) back to Draft (S22).

    Delegates to `ps_service.policy_lifecycle.service.revert_policy_to_draft`
    (L2 "delegate, don't reimplement") -- this tool resolves the caller's
    identity and the policy graph handle, then does nothing else. Uses the
    exact same `_resolve_policy_lifecycle_actor` identity resolution as
    `create-policy-draft`/`get-policy`/`propose-policy`/`approve-policy`/
    `reject-policy` (D-11): the local-test bypass DOES count as a real
    authenticated caller here.

    Owner-only (AC-BI-002/007): unlike `reject-policy`, this action has NO
    `PolicyManager` RBAC gate at all -- only the Policy's own owner may
    revert it, and only while it is currently `"proposed"`. A `PolicyManager`
    who is not the owner is rejected exactly like any other non-owner; no
    `access_role_store` is even constructed for this tool, mirroring
    `propose-policy`'s own owner-only wrapper shape rather than
    `approve-policy`/`reject-policy`'s `PolicyManager`-gated shape.

    On success, returns `{"policy_id", "status", "standard_ids",
    "control_ids"}`. Returns a string beginning `error: ` when the caller
    has no real authenticated session, when no Policy exists with
    `policy_id`, when the caller does not own it, when it is not currently
    `"proposed"`, when the policy graph cannot be reached, or (this tool's
    own residual safety net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        audit_store = PsycopgAuditStore(config)
        try:
            result = run_revert_policy_to_draft(
                actor=actor, policy_id=policy_id, graph=graph, audit_store=audit_store
            )
        except _REVERT_POLICY_TO_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "status": result.status,
            "standard_ids": list(result.standard_ids),
            "control_ids": list(result.control_ids),
        }

    return _run_mcp_action("revert_policy_to_draft", principal, _body)


@server.tool(name="find-capability-merge-candidates")
def find_capability_merge_candidates_tool(
    min_similarity: Annotated[float | None, Field(gt=0.5, le=1.0)] = None,
) -> dict[str, object] | str:
    """FindCapabilityMergeCandidates: list groups of active Capabilities that look like duplicates.

    Compliance Officer graph cleanup (issue #190), read-only: nothing is
    changed and no approval is created. Requires an explicit `ComplianceOfficer`
    grant on a real authenticated session (no admin override, never available
    under the local-test bypass).

    `min_similarity` (greater than 0.5, at most 1.0) is the cosine similarity
    of the Capabilities' already-cached embeddings at or above which two are
    linked; when omitted, the configured Company Merge threshold applies, else
    0.90. No embedding is fetched or computed by this tool.

    Returns `{"groups": [{"basis", "merge_case", "policies_distinct", "members":
    [{"id", "name", "obligation_count", "governing_policy"}, ...]}, ...]}`;
    `basis` is `"name"` when every member's name is equal after case and
    punctuation are ignored, else `"embedding"`. `governing_policy` is
    `{"id", "title", "status"}` or `null`; `obligation_count` is the number of
    Obligations requiring that Capability. `merge_case` is 1 (no member governed),
    2 (exactly one governed) or 3 (two or more governed); `policies_distinct` is
    true when the governed members name different Policies. Returns a string
    beginning `error: ` when the
    caller has no real authenticated session or lacks the `ComplianceOfficer`
    role, when the authorization store or the policy graph cannot be reached,
    or (this tool's own residual safety net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        actor = _require_cleanup_actor(config)
        if isinstance(actor, str):
            return actor
        dependencies = build_default_graph_cleanup_dependencies()
        try:
            graph = _sanitize_graph_open(dependencies.open_single_tenant_graph)(config)
            result = find_capability_merge_candidates(
                graph,
                min_similarity=resolve_min_similarity(
                    min_similarity, config.company_merge_similarity_threshold
                ),
            )
        except McpGraphUnavailableError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        except GraphCleanupPersistenceError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return result.model_dump()

    return _run_mcp_action("find_capability_merge_candidates", principal, _body)


@server.tool(name="find-duplicate-obligations")
def find_duplicate_obligations_tool(role_id: str | None = None) -> dict[str, object] | str:
    """FindDuplicateObligations: list groups of duplicate Obligations under one Role.

    Compliance Officer graph cleanup (issue #190), read-only: nothing is
    changed and no approval is created. Requires an explicit `ComplianceOfficer`
    grant on a real authenticated session (no admin override, never available
    under the local-test bypass).

    Only Obligations under the same Role are grouped, and only when their text
    is identical or near-identical once case and punctuation are ignored; the
    same wording under two Roles is never grouped. `role_id`, when given, limits
    the sweep to that Role.

    Returns `{"groups": [{"role_id", "role_name", "basis", "members": [{"id",
    "text", "requirements": [{"requirement_id", "source_ref"}, ...]}, ...]},
    ...]}`; `basis` is `"identical_text"` or `"near_text"`. `source_ref` comes
    from the `EXPRESSES` edge of each Requirement the Obligation satisfies.
    Returns a string beginning `error: ` when the caller has no real
    authenticated session or lacks the `ComplianceOfficer` role, when the
    authorization store or the policy graph cannot be reached, or (this tool's
    own residual safety net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        actor = _require_cleanup_actor(config)
        if isinstance(actor, str):
            return actor
        dependencies = build_default_graph_cleanup_dependencies()
        try:
            graph = _sanitize_graph_open(dependencies.open_single_tenant_graph)(config)
            result = find_duplicate_obligations(graph, role_id=role_id)
        except McpGraphUnavailableError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        except GraphCleanupPersistenceError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return result.model_dump()

    return _run_mcp_action("find_duplicate_obligations", principal, _body)


@server.tool(name="merge-capabilities")
def merge_capabilities_tool(
    survivor_id: Annotated[str, Field(min_length=1)],
    absorbed_id: Annotated[str, Field(min_length=1)],
    ctx: Context,
    *,
    acknowledge_governance_change: bool = False,
) -> dict[str, object] | str:
    """MergeCapabilities: preview merging `absorbed_id` into `survivor_id` and request approval.

    Compliance Officer graph cleanup (issue #190). Requires an explicit `ComplianceOfficer`
    grant on a real authenticated session (no admin override, never available under the
    local-test bypass). This call NEVER edits the graph: it returns a preview and creates
    a pending passkey approval bound to this exact pair and to the state previewed. The
    merge executes only when the officer opens `approval_url` in a browser and signs with a
    passkey; `check-cleanup-approval` reports the outcome.

    On execution the absorbed Capability's `REQUIRES`, `COVERS` and `MITIGATED_BY` edges move
    to the survivor with no duplicate edges, and the absorbed node is kept as a tombstone
    (`status` `merged`, `MERGED_INTO` the survivor), in one all-or-nothing write that is
    audited first (`capability.merge`).

    Merge case 1 (neither Capability has a governing Policy) needs nothing more. Case 2 (exactly
    one is governed) changes which Capability that Policy governs: the first call, without
    `acknowledge_governance_change`, returns the preview with `acknowledgment_required: true`
    and a `message`, and creates NO approval; repeat the call with
    `acknowledge_governance_change=true` after the Compliance Officer has explicitly accepted
    that change, and the acknowledgment becomes part of the signed approval and the audit row
    (`policy_case` 2). The preview's `governance` block names the policy (`id`, `title`,
    `status`), `obligations_coverage_changed`, and the policy's governed set before and after;
    an `approved` policy keeps its content and version. Case 3 (both governed by the SAME
    policy) needs no acknowledgment: the absorbed capability's `GOVERNED_BY` edge is deleted and
    the survivor's stays (`policy_case` 3). Two Capabilities governed by DIFFERENT policies are
    rejected before any approval, with an error naming both policies and the
    `release-capability-governance` step; when neither policy is a draft that step is not
    available and the error states there is no completion path (a policy fork carries the whole
    governed set).

    Returns `{"preview": {"survivor_id", "survivor_name", "absorbed_id", "absorbed_name",
    "policy_case", "edges_to_move": {"requires", "covers", "mitigated_by"},
    "duplicate_edges_collapsed", "obligations_affected", "state_digest", "governance"},
    "pending_approval_id", "approval_url", "expires_at"}` (`governance` is `null` in case 1),
    or, for case 2 without the acknowledgment, `{"preview", "acknowledgment_required": true,
    "message"}`. Returns a string beginning
    `error: ` when the caller is not an authenticated Compliance Officer, when a side
    is the same node, does not exist, is a `merged` tombstone or is not active, when the two
    are governed by different policies, when the policy
    graph database cannot be reached, or (this tool's own residual safety net) on any
    other unexpected failure. No approval is created on any error.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        actor = _require_cleanup_actor(config)
        if isinstance(actor, str):
            return actor
        dependencies = build_default_graph_cleanup_dependencies()
        try:
            graph = _sanitize_graph_open(dependencies.open_single_tenant_graph)(config)
            approval = create_capability_merge_approval(
                graph,
                survivor_id=survivor_id,
                absorbed_id=absorbed_id,
                acknowledge_governance_change=acknowledge_governance_change,
                actor=actor,
                base_url=_resolve_base_url(ctx),
                store=PsycopgPendingApprovalStore(config),
            )
        except GraphCleanupAcknowledgmentRequiredError as exc:
            return {
                "preview": exc.preview.model_dump(mode="json"),
                "acknowledgment_required": True,
                "message": str(exc),
            }
        except GraphCleanupValidationError as exc:
            return f"error: {exc}"
        except McpGraphUnavailableError, GraphCleanupPersistenceError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return {
            "preview": approval.preview.model_dump(mode="json"),
            "pending_approval_id": approval.pending_approval_id,
            "approval_url": approval.approval_url,
            "expires_at": approval.expires_at,
        }

    return _run_mcp_action("merge_capabilities", principal, _body)


@server.tool(name="merge-obligations")
def merge_obligations_tool(
    survivor_id: Annotated[str, Field(min_length=1)],
    absorbed_id: Annotated[str, Field(min_length=1)],
    ctx: Context,
) -> dict[str, object] | str:
    """MergeObligations: preview merging `absorbed_id` into `survivor_id` and request approval.

    Compliance Officer graph cleanup (issue #190). Requires an explicit `ComplianceOfficer`
    grant on a real authenticated session (no admin override, never available under the
    local-test bypass). This call NEVER edits the graph: it returns a preview and creates a
    pending passkey approval bound to this exact pair and to the state previewed. The merge
    executes only when the officer opens `approval_url` in a browser and signs with a passkey;
    `check-cleanup-approval` reports the outcome.

    Both Obligations must be borne by the SAME Role. On execution the absorbed Obligation's
    `SATISFIED_BY` and `REQUIRES` edges union onto the survivor (no duplicate edges), the
    survivor keeps its single `HAS` Role edge, and the absorbed Obligation is DELETED with its
    full node and edge snapshot held in the audit row (`obligation.merge`), all in one
    all-or-nothing write that is audited first. The delete leaves a `MergedObligation` marker
    so a later ingest or restore that regenerates the absorbed Obligation attaches to the
    survivor instead of recreating it.

    Returns `{"preview": {"survivor_id", "survivor_text", "absorbed_id", "absorbed_text",
    "role_id", "role_name", "edges_to_move": {"satisfied_by", "requires"},
    "duplicate_edges_collapsed", "requirement_source_refs": [{"requirement_id",
    "source_ref"}], "state_digest"}, "pending_approval_id", "approval_url", "expires_at"}`.
    Returns a string beginning `error: ` when the caller is not an authenticated Compliance
    Officer, when a side is the same node or does not exist, when the two Obligations are
    under different Roles, when the policy graph database cannot be reached, or (this tool's
    own residual safety net) on any other unexpected failure. No approval is created on any
    error.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        actor = _require_cleanup_actor(config)
        if isinstance(actor, str):
            return actor
        dependencies = build_default_graph_cleanup_dependencies()
        try:
            graph = _sanitize_graph_open(dependencies.open_single_tenant_graph)(config)
            approval = create_obligation_merge_approval(
                graph,
                survivor_id=survivor_id,
                absorbed_id=absorbed_id,
                actor=actor,
                base_url=_resolve_base_url(ctx),
                store=PsycopgPendingApprovalStore(config),
            )
        except GraphCleanupValidationError as exc:
            return f"error: {exc}"
        except McpGraphUnavailableError, GraphCleanupPersistenceError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return {
            "preview": approval.preview.model_dump(mode="json"),
            "pending_approval_id": approval.pending_approval_id,
            "approval_url": approval.approval_url,
            "expires_at": approval.expires_at,
        }

    return _run_mcp_action("merge_obligations", principal, _body)


@server.tool(name="release-capability-governance")
def release_capability_governance_tool(
    capability_id: Annotated[str, Field(min_length=1)],
    ctx: Context,
) -> dict[str, object] | str:
    """ReleaseCapabilityGovernance: preview releasing a Capability from its draft policy.

    Compliance Officer graph cleanup (issue #190). Requires an explicit `ComplianceOfficer`
    grant on a real authenticated session (no admin override, never available under the
    local-test bypass). This call NEVER edits the graph: it returns a preview and creates a
    pending passkey approval bound to this capability, its governing policy and the state
    previewed. The release executes only when the officer opens `approval_url` in a browser
    and signs with a passkey; `check-cleanup-approval` reports the outcome.

    Only a Capability governed by a `draft` Policy can be released: on execution its single
    `GOVERNED_BY` edge to that Policy is deleted (the Capability and the Policy stay, the
    Capability becomes ungoverned) in one guarded write that is audited first
    (`capability.release_governance`, with a before/after snapshot). A `proposed` Policy is
    rejected with a pointer to `revert-policy-to-draft`; an `approved` or `deprecated` Policy
    is rejected with a pointer to the policy lifecycle, stating plainly that a fork carries
    the whole governed set and so does not by itself free the Capability.

    Returns `{"preview": {"capability_id", "capability_name", "policy_id", "policy_title",
    "policy_status", "governed_set_before", "governed_set_after", "state_digest"},
    "pending_approval_id", "approval_url", "expires_at"}`. Returns a string beginning
    `error: ` when the caller is not an authenticated Compliance Officer, the Capability does
    not exist, is a `merged` tombstone or not active, is not governed, or its governing policy
    is not a draft, when the policy graph database cannot be reached, or (this tool's own
    residual safety net) on any other unexpected failure. No approval is created on any error.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        actor = _require_cleanup_actor(config)
        if isinstance(actor, str):
            return actor
        dependencies = build_default_graph_cleanup_dependencies()
        try:
            graph = _sanitize_graph_open(dependencies.open_single_tenant_graph)(config)
            approval = create_release_governance_approval(
                graph,
                capability_id=capability_id,
                actor=actor,
                base_url=_resolve_base_url(ctx),
                store=PsycopgPendingApprovalStore(config),
            )
        except GraphCleanupValidationError as exc:
            return f"error: {exc}"
        except McpGraphUnavailableError, GraphCleanupPersistenceError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return {
            "preview": approval.preview.model_dump(mode="json"),
            "pending_approval_id": approval.pending_approval_id,
            "approval_url": approval.approval_url,
            "expires_at": approval.expires_at,
        }

    return _run_mcp_action("release_capability_governance", principal, _body)


_AUDIT_UNREADABLE_MESSAGE = "error: the audit trail could not be read right now; try again shortly"


@server.tool(name="unmerge")
def unmerge_tool(
    merged_id: Annotated[str, Field(min_length=1)],
    ctx: Context,
) -> dict[str, object] | str:
    """Unmerge: preview reversing a Compliance Officer merge and request approval (issue #190).

    Requires an explicit `ComplianceOfficer` grant on a real authenticated session (no admin
    override, never available under the local-test bypass). `merged_id` is the id that was
    absorbed by `merge-capabilities` (a `merged` Capability tombstone) or by `merge-obligations`
    (a deleted Obligation). The merge is found in the audit trail (`capability.merge` or
    `obligation.merge`): the newest `applied` row not followed by a `failed` row for the same
    approval. This call NEVER edits the graph: it returns a preview and creates a pending passkey
    approval bound to the merge it reverses and to the state previewed. The unmerge executes only
    when the officer opens `approval_url` in a browser and signs with a passkey;
    `check-cleanup-approval` reports the outcome.

    Capability merge: the tombstone returns to `active` with its `MERGED_INTO` edge removed and
    exactly the edges the merge moved off it are restored from the audit snapshot (the survivor
    keeps any edge it already had before the merge). Edges added to the survivor since the merge
    stay in place and are listed as `survivor_added_edges`.

    Obligation merge: the deleted Obligation is recreated under its original id with its
    properties, its `HAS` edge from the Role and exactly its `SATISFIED_BY` and `REQUIRES` edges
    from the audit snapshot, and its `MergedObligation` marker is removed. The survivor's edges are
    never removed (a union cannot be attributed): edges added since the merge are listed as
    `survivor_added_edges` and those that may have come from the absorbed Obligation as
    `survivor_edges_possibly_from_merge` (they may originate from the merge).

    Either way the write is one guarded all-or-nothing statement that is audited first
    (`capability.unmerge` or `obligation.unmerge`, with a before/after snapshot).

    Returns `{"preview": {"kind": "capability" | "obligation", "merged_id", "survivor_id",
    "merge_approval_id", "edges_to_restore", "survivor_added_edges", "state_digest", ...},
    "pending_approval_id", "approval_url", "expires_at"}`; a capability preview also carries
    `merged_name`, `survivor_name` and `edges_removed_from_survivor`, an obligation preview
    `merged_text`, `survivor_text`, `role_id`, `survivor_edges_possibly_from_merge` and `note`.
    Returns a string beginning `error: ` when the caller is not an authenticated Compliance
    Officer, no merge of the id is in the audit trail, the merge cannot be reversed because of a
    conflict (the survivor was merged away or re-pointed, its governance changed, a restored
    edge's endpoint is gone, the Obligation exists again; each is explained), the audit trail or
    the policy graph database cannot be reached, or (this tool's own residual safety net) on any
    other unexpected failure. No approval is created on any error.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        actor = _require_cleanup_actor(config)
        if isinstance(actor, str):
            return actor
        dependencies = build_default_graph_cleanup_dependencies()
        try:
            graph = _sanitize_graph_open(dependencies.open_single_tenant_graph)(config)
            approval = create_unmerge_approval(
                graph,
                dependencies.audit_store(config),
                merged_id=merged_id,
                actor=actor,
                base_url=_resolve_base_url(ctx),
                store=PsycopgPendingApprovalStore(config),
            )
        except GraphCleanupValidationError as exc:
            return f"error: {exc}"
        except McpGraphUnavailableError, GraphCleanupPersistenceError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        except AuditPostgresUnavailableError, AuditPersistenceError:
            return _AUDIT_UNREADABLE_MESSAGE
        return {
            "preview": approval.preview.model_dump(mode="json"),
            "pending_approval_id": approval.pending_approval_id,
            "approval_url": approval.approval_url,
            "expires_at": approval.expires_at,
        }

    return _run_mcp_action("unmerge", principal, _body)


_CLEANUP_APPROVAL_UNSETTLED_MESSAGE = (
    "error: the approval status could not be settled right now; try again shortly"
)


@server.tool(name="check-cleanup-approval")
def check_cleanup_approval_tool(
    pending_approval_id: Annotated[str, Field(min_length=1)],
) -> dict[str, object] | str:
    """CheckCleanupApproval: status and outcome of a graph-cleanup passkey approval (issue #190).

    Companion to `merge-capabilities`, `merge-obligations`, `release-capability-governance` and
    `unmerge`:
    the passkey ceremony happens in a browser, so this is the way to learn whether the approval
    was signed and what the edit did. Requires an explicit `ComplianceOfficer` grant. Only the
    officer who created the approval can see it; any other caller, or an unknown id, gets the
    same not-found error.

    `status` is `pending`, `expired` (the window elapsed unsigned, derived live) or `signed`.
    `outcome` is `null` until the signed approval has run, then either the result
    (`{"survivor_id", "absorbed_id", "merged": true}` for either merge tool,
    `{"capability_id", "policy_id", "released": true}` for a release,
    `{"merged_id", "survivor_id", "unmerged": true, "survivor_added_edges"}` for an unmerge
    of either kind),
    `{"error": <message>}`, or `{"reconciled": "applied"}`. A signed approval whose outcome
    was never recorded is checked against the graph once it is more than five minutes past
    its expiry and settled: if the
    edit is present the outcome becomes `reconciled`; if not, a `failed` audit row is
    recorded and the outcome becomes an error. An `applied` audit row followed by a `failed`
    row for the same approval id means no edit occurred.

    Returns `{"pending_approval_id", "status", "tool_name", "outcome"}`, or a string beginning
    `error: ` when the caller is not an authenticated Compliance Officer, no such approval is
    visible to the caller, settling it was not possible right now, or (this tool's own
    residual safety net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        actor = _require_cleanup_actor(config)
        if isinstance(actor, str):
            return actor
        try:
            status = check_cleanup_approval(
                pending_approval_id=pending_approval_id,
                actor=actor,
                store=PsycopgPendingApprovalStore(config),
                config=config,
                dependencies=_sanitize_cleanup_graph_open(
                    build_default_graph_cleanup_dependencies()
                ),
            )
        except (
            McpGraphUnavailableError,
            GraphCleanupPersistenceError,
            AuditPostgresUnavailableError,
            AuditPersistenceError,
        ):
            return _CLEANUP_APPROVAL_UNSETTLED_MESSAGE
        if status is None:
            return f"error: no pending approval with id {pending_approval_id!r}"
        return {
            "pending_approval_id": status.pending_approval_id,
            "status": status.status,
            "tool_name": status.tool_name,
            "outcome": status.outcome,
        }

    return _run_mcp_action("check_cleanup_approval", principal, _body)
