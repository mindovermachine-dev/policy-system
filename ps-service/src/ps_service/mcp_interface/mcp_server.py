#!/usr/bin/env python3
"""MCP server surface of the PS Service: the shared `MCPServer` and its tool plumbing.

Defines `server`, the single `MCPServer` instance on which every PS tool and the
`psdomain://concepts` resource are registered, and the helpers all tools share: caller-identity
resolution from the verified bearer token, role gating, graph-opener sanitising and the
started/succeeded/failed log triad wrapped around every tool body. Read-only graph queries run
in-process through `ps_service.query_engine.execute_cypher_query`; the write-clause guard and query
execution live in Query Engine and are never duplicated here.

Business Rules:
    - This module binds no transport and no authentication. The Streamable HTTP transport in the
      sibling `http_transport` module is the only transport and arms authentication on `server` from
      outside this file, so importing this module performs no I/O and no fail-closed check.
    - A tool failure crosses the MCP boundary as an `error: <message>` string. Infrastructure
      failures use fixed, detail-free wording: no host, port, driver or credential text.
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
    GraphWriteUnavailableError,
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
from ps_service.api.graph_write_access import graph_write_access
from ps_service.api.ingestion_orchestration import (
    _STAGE_REASON_MAX_LEN,  # pyright: ignore[reportPrivateUsage]  -- shared failure-reason cap, reused for `_run_mcp_action`'s own truncation; mirrors change_check_orchestration.py's cross-module private-import convention
    GraphOpeners,
    _require_ingestion_config,  # pyright: ignore[reportPrivateUsage]  -- the identical config-completeness check, reused so the error text can never drift
    build_default_pipeline_dependencies,
    classify_ingestion_failure,
    resolve_ingestion_entry,
    run_audited_catalog_ingestion,
    run_catalog_ingestion_pipeline,
)
from ps_service.api.ingestion_orchestration import (
    SHORT_NAME_PATTERN as _SHORT_NAME_PATTERN,  # shared with api/models.py's short_name
)
from ps_service.api.models import (
    CatalogInstrumentEntry,
    CatalogRestorationRequest,
    CuratedCatalogResponse,
    IngestionRunStatusResponse,
    StartIngestionResponse,
)
from ps_service.api.near_miss_review_orchestration import (
    build_default_near_miss_review_dependencies,
    check_near_miss_merge_approval,
    run_list_near_misses,
    run_request_near_miss_merge,
    run_resolve_near_miss,
)
from ps_service.api.restore_orchestration import (
    build_default_restore_from_catalog_dependencies,
    run_restoration_from_catalog_source,
)
from ps_service.api.routes import (
    _to_accepted_response,  # pyright: ignore[reportPrivateUsage]  -- reuses the REST wire-shaping helper verbatim so the MCP and REST paths can never silently drift; mirrors change_check_orchestration.py's cross-module private-import convention
    _to_change_check_response,  # pyright: ignore[reportPrivateUsage]  -- same reuse for `check_regulations`, mirroring `_to_accepted_response` immediately above
)
from ps_service.api.run_status import get_stage
from ps_service.api.stage_failure_log import stage_failure_detail
from ps_service.audit.actor import resolve_audit_actor
from ps_service.audit.emit import AuditContext, AuditTarget, record_follow_up_row
from ps_service.audit.errors import (
    AuditActorUnresolvedError,
    AuditPersistenceError,
    AuditPostgresUnavailableError,
    AuditTrailUnavailableError,
)
from ps_service.audit.models import AuditQueryFilters
from ps_service.audit.store import PsycopgAuditStore
from ps_service.authz.models import AccessRole
from ps_service.authz.service import (
    grant_role,
    list_assignments,
    revoke_role,
)
from ps_service.authz.service import (
    list_audit_events as run_list_audit_events,
)
from ps_service.authz.service import (
    verify_audit_log as run_verify_audit_log,
)
from ps_service.authz.store import PsycopgAccessRoleStore
from ps_service.config import LOCAL_TEST_PRINCIPAL_ID, ServiceConfigurationError, load_config
from ps_service.curated_source import store as catalog_source_store
from ps_service.curated_source.catalog_client import build_default_curated_catalog_dependencies
from ps_service.curated_source.errors import CuratedSourceFetchError
from ps_service.curated_source.resolve import resolve_effective_source
from ps_service.domain_schema import DOMAIN_SCHEMA, render_slim_schema
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
from ps_service.ingestion_runs import IngestionRunStoreError, PsycopgIngestionRunStore
from ps_service.ingestion_runs.audit_actions import INGESTION_RUN_RECONCILER_ACTOR
from ps_service.ingestion_runs.dispatch import (
    is_run_in_flight,
    release_run_slot,
    reserve_run_slot,
    start_background_run,
)
from ps_service.ingestion_runs.errors import (
    IngestionRunAlreadyInProgressError,
    IngestionRunCapacityExceededError,
)
from ps_service.invitations.client import create_invitation
from ps_service.invitations.errors import AuthentikInvitationError
from ps_service.invitations.service import invite_user_audited
from ps_service.logging import (
    LoggingLifecycleError,
    bind_run_context,
    current_run_id,
    emit_log_entry,
)
from ps_service.mcp_interface.audit_actions import (
    QUERY_CYPHER_ACTION,
    QUERY_CYPHER_RESOURCE_ID,
    QUERY_RESOURCE_TYPE,
    QueryCypherReasonCode,
)
from ps_service.mcp_interface.errors import (
    McpGraphUnavailableError,
    McpResourceUnavailableError,
)
from ps_service.mcp_interface.tool_gates import (
    BypassPolicy,
    GateEnvironment,
    GateKind,
    ToolGate,
    gated_tool,
    lifecycle_role_enforcer,
    run_body_gate,
)
from ps_service.passkey_signing.store import PsycopgPendingApprovalStore
from ps_service.passkey_signing.webauthn_rp import signable_link_host
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
from ps_service.policy_lifecycle.graph_submission import lifecycle_submitter
from ps_service.policy_lifecycle.service import (
    _CONTROL_IMPLEMENTATION_STATUS_VALUES,  # pyright: ignore[reportPrivateUsage] -- same drift-avoidance reuse as `_POLICY_PATCHABLE_FIELDS`
    _CONTROL_PATCHABLE_FIELDS,  # pyright: ignore[reportPrivateUsage] -- same drift-avoidance reuse as `_POLICY_PATCHABLE_FIELDS`
    _POLICY_PATCHABLE_FIELDS,  # pyright: ignore[reportPrivateUsage] -- declared once in service.py, imported here to avoid drift between the two layers
    _STANDARD_IMPLEMENTATION_STATUS_VALUES,  # pyright: ignore[reportPrivateUsage] -- same drift-avoidance reuse as `_POLICY_PATCHABLE_FIELDS`
    _STANDARD_PATCHABLE_FIELDS,  # pyright: ignore[reportPrivateUsage] -- same drift-avoidance reuse as `_POLICY_PATCHABLE_FIELDS`
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

    from ps_service.api.catalog_entry import CatalogEntry
    from ps_service.api.change_check_orchestration import ChangeCheckDependencies
    from ps_service.api.ingestion_orchestration import PipelineDependencies
    from ps_service.api.near_miss_review_orchestration import NearMissReviewDependencies
    from ps_service.api.restore_orchestration import CatalogRestoreDependencies
    from ps_service.audit.models import AuditChainVerification, AuditEventRow
    from ps_service.authz.models import AccessRoleAssignmentRow
    from ps_service.config import ServiceConfig
    from ps_service.curated_source.catalog_client import CuratedCatalogDependencies
    from ps_service.ingestion.adapters.base import IngestionAdapter
    from ps_service.ingestion_runs import IngestionRunRow, IngestionRunStore
    from ps_service.ingestion_runs.audit_actions import IngestionReasonCode, IngestionTrigger
    from ps_service.logging.emitter import LogEmitter

_COMPONENT = "mcp_interface"
_ACTION = "handle_mcp_tool_call"
_DEFAULT_GRAPH_NAME = "policy_system"
_DOMAIN_CONCEPTS_URI = "psdomain://concepts"
_DOMAIN_CONCEPTS_UNAVAILABLE_DETAIL = "the ps-domain-concepts resource is currently unavailable"
_GRAPH_UNAVAILABLE_DETAIL = "the policy graph database is not reachable"
_GRAPH_UNAVAILABLE_MESSAGE = f"error: {_GRAPH_UNAVAILABLE_DETAIL}"
_UNEXPECTED_ERROR_MESSAGE = "error: an unexpected error occurred"
# Same wording ps-cli uses for this condition, so both client paths read alike to operators.
_LLM_INTERFACE_UNAVAILABLE_MESSAGE = "error: LLM Interface is unavailable."

# `_SHORT_NAME_PATTERN` is `SHORT_NAME_PATTERN` from `ps_service.api.ingestion_orchestration`:
# a letter first, then alphanumerics, `_` or `-`, 1-64 characters in all (as "cra", "gdpr" and
# "nis2"). It is one pattern literal shared with `api/models.py`'s
# `CatalogIngestionRequest.short_name`, so REST and MCP accept the same names.
# Same CELEX pattern `api/models.py`'s `CatalogIngestionRequest.celex` already enforces.
_CELEX_PATTERN = r"^3\d{4}[A-Z]\d{4}$"
# Charset and length bound for a curated instrument id, the same as ps-cli's own instrument-id
# pattern and at least as strict as `CatalogRestorationRequest.instrument_id`'s REST pattern,
# which has no `..` guard of its own. The `..` rejection is a separate `AfterValidator`
# (`_reject_path_traversal_segment`) rather than a look-ahead in this regex, because
# pydantic-core's regex backend (the Rust `regex` crate) does not support look-around and raises
# a `SchemaError` at tool-registration time. The validator still runs inside pydantic's
# schema-validation pass, before the tool body, mirroring ps-cli's regex-then-`..` two-step check.
_RESTORE_INSTRUMENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
# A deliberately permissive structural "looks like an email" check for `invite_user`. Full
# RFC 5322 validation is out of scope: `pydantic.EmailStr` needs the `email-validator` package,
# which is not an installed dependency, and the real validation authority is Authentik itself
# at redemption time.
_INVITE_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def _reject_path_traversal_segment(value: str) -> str:
    """Reject an instrument id that contains `..`.

    Runs as an `AfterValidator` once `_RESTORE_INSTRUMENT_ID_PATTERN` has matched, inside pydantic's
    schema-validation pass, so the check happens before `restore_instrument`'s body runs. It is a
    separate validator because the regex backend cannot express the rule.

    Args:
        value: An instrument id that already matched the charset and length pattern.

    Returns:
        `value`, unchanged.

    Raises:
        ValueError: `value` contains `..`.
    """
    if ".." in value:
        msg = "instrument_id must not contain '..'"
        raise ValueError(msg)
    return value


@functools.cache
def _domain_concepts_path() -> Traversable:
    """Locate the packaged `ps-domain-concepts.md` resource file.

    Resolved with `importlib.resources` against the installed package, so it works from a wheel
    install as well as from a checkout. Lazy and cached: nothing is touched at import time.

    Returns:
        A `Traversable` for the file inside `ps_service.mcp_interface`; its existence is not
        checked.
    """
    return resources.files("ps_service.mcp_interface").joinpath("ps-domain-concepts.md")


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

# The gate wrapper's collaborators, each looked up when called so the names this module rebinds
# (the store classes, the configuration loader) stay the single seam tests and deployments patch.
_gate_environment = GateEnvironment(
    load_config=lambda: load_config(),  # noqa: PLW0108
    resolve_principal=lambda config: _resolve_principal(config),  # noqa: PLW0108
    resolve_actor=lambda config: _resolve_authz_actor(config),  # noqa: PLW0108
    role_store_factory=lambda config: PsycopgAccessRoleStore(
        config, audit_store=PsycopgAuditStore(config)
    ),
    audit_store_factory=lambda config: PsycopgAuditStore(config),  # noqa: PLW0108
    run_action=lambda action, principal, body: _run_mcp_action(action, principal, body),  # noqa: PLW0108
)
_gated_tool = gated_tool(server, _gate_environment)


@dataclasses.dataclass(frozen=True, slots=True)
class CypherCallOutcome:
    """The result of one in-process `cypher` run plus the facts its audit row records.

    Attributes:
        response: What the tool returns to the caller: the result dict, or an `error: ...` string.
        row_count: Rows returned, or `None` when the query did not run to completion.
        truncated: Whether the row cap cut the result, or `None` when it did not complete.
        reason_code: Why the call did not complete (`write_clause`, `graph_unseeded` or
            `query_failed`), or `None` when it succeeded. Never exception or query text.
    """

    response: dict[str, object] | str
    row_count: int | None
    truncated: bool | None
    reason_code: QueryCypherReasonCode | None


def run_cypher_for_tool(
    query: str,
    *,
    graph: GraphHandle,
    emitter: LogEmitter | None = None,
    principal: str | None = None,
    timeout_ms: int,
    row_cap: int,
) -> CypherCallOutcome:
    """Run one read-only Cypher `query` in-process and classify how the call ended.

    The shared core of `handle_mcp_tool_call` and the `cypher` tool: the tool needs the outcome
    class for its audit row, while `handle_mcp_tool_call` returns only the response. This function
    writes no audit row and neither inspects nor rewrites `query`; Query Engine owns the
    write-clause guard.

    Args:
        query: Cypher text to execute; it must be read-only.
        graph: Handle of the graph to query.
        emitter: Log emitter forwarded to Query Engine; `None` leaves the choice to Query Engine.
        principal: Opaque caller identity recorded on the `query_engine` log entry; omitted when
            `None`.
        timeout_ms: Server-side execution bound in milliseconds, forwarded unchanged.
        row_cap: Maximum number of rows returned, forwarded unchanged.

    Returns:
        A `CypherCallOutcome`. On success `response` is `{"columns", "rows", "row_count",
        "truncated"}`; otherwise it is the `error: <message>` string, verbatim from Query Engine.

    Raises:
        LoggingLifecycleError: No log emitter is available (none given and no process default).

    Side Effects:
        Binds a run context for the call's duration. Query Engine emits one log entry carrying the
        run id and `principal`. Nothing is written to the graph or the audit trail.
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
        except WriteClauseRejectedError as exc:
            return CypherCallOutcome(f"error: {exc}", None, None, "write_clause")
        except GraphUnseededError as exc:
            return CypherCallOutcome(f"error: {exc}", None, None, "graph_unseeded")
        except QueryEngineExecutionError as exc:
            return CypherCallOutcome(f"error: {exc}", None, None, "query_failed")
    response: dict[str, object] = {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "truncated": result.truncated,
    }
    return CypherCallOutcome(response, result.row_count, result.truncated, None)


def handle_mcp_tool_call(
    query: str,
    *,
    graph: GraphHandle,
    emitter: LogEmitter | None = None,
    principal: str | None = None,
    timeout_ms: int,
    row_cap: int,
) -> dict[str, object] | str:
    """Run one read-only Cypher `query` through Query Engine in-process, as `cypher` does.

    Binds a fresh run id for the call, then executes the query with the given bounds. Query Engine
    owns the write-clause guard, so this layer neither inspects nor rewrites `query`.

    Args:
        query: Cypher text to execute; it must be read-only.
        graph: Handle of the graph to query.
        emitter: Log emitter forwarded to Query Engine for its log entry; `None` leaves the choice
            to Query Engine.
        principal: Opaque caller identity recorded on the `query_engine` log entry; the field is
            omitted from the entry when `None`.
        timeout_ms: Server-side execution bound in milliseconds, forwarded unchanged.
        row_cap: Maximum number of rows returned, forwarded unchanged; `truncated` reports whether
            it cut the result.

    Returns:
        `{"columns", "rows", "row_count", "truncated"}` on success (`rows` may be empty), or an
        `error: <message>` string, verbatim from Query Engine, for a rejected write clause, an
        unseeded graph or a failed query.

    Raises:
        LoggingLifecycleError: No log emitter is available (none given and no process default).

    Side Effects:
        Binds a run context for the call's duration. Query Engine emits one log entry carrying the
        run id and `principal`. Nothing is written to the graph.
    """
    return run_cypher_for_tool(
        query,
        graph=graph,
        emitter=emitter,
        principal=principal,
        timeout_ms=timeout_ms,
        row_cap=row_cap,
    ).response


def _resolve_graph(config: ServiceConfig) -> GraphHandle:
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


def _resolve_principal(config: ServiceConfig) -> str | None:
    """Resolve the caller identity recorded on a tool call's log entries.

    A verified bearer token's subject always wins: `get_access_token()` reads the token the MCP
    SDK's authentication middleware stored for this request, which the SDK installs whenever the
    transport has a token verifier. Without a token, the fixed local-test principal is used only
    while the local-test bypass is active. Otherwise there is no principal; that case is unreachable
    in practice because `ps_service.main.create_app` fails closed at startup when neither real
    authentication nor the bypass is configured, but this function does not rely on it.

    Args:
        config: Service configuration; only `is_local_test_bypass_active` is read.

    Returns:
        The bearer token's subject (`None` if the token carries none), `LOCAL_TEST_PRINCIPAL_ID`
        when there is no token and the bypass is active, otherwise `None`.
    """
    access_token = get_access_token()
    if access_token is not None:
        return access_token.subject
    if config.is_local_test_bypass_active:
        return LOCAL_TEST_PRINCIPAL_ID
    return None


def _resolve_signing_actor() -> tuple[str, str] | None:
    """Resolve the `(sub, iss)` identity a passkey-signed approval is bound to.

    A signed approval is bound to both subject and issuer, not subject alone, which mirrors the
    `sub`/`iss` extraction `RestAuthMiddleware.__call__` performs on the same `AccessToken` shape.
    Unlike `_resolve_principal` there is deliberately no fallback to `LOCAL_TEST_PRINCIPAL_ID`:
    under the local-test bypass no real actor exists that a WebAuthn credential could belong to, so
    no pending approval may be created. Callers treat `None` as a fail-closed condition.

    Returns:
        The verified `(sub, iss)` pair, or `None` if no verified bearer token is bound to this call,
        the token has no subject, or its `iss` claim is not a string.
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

# Defensive fallback for a non-bypass call with no verified bearer token bound to it. It is
# unreachable in practice (`ps_service.main.create_app` fails closed at startup whenever no real
# authentication is configured and the bypass is off, the invariant `_resolve_principal`
# documents), but it keeps the gate fail-closed rather than passing `None` through to
# the role gate should that invariant ever be violated.
_CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE = (
    "error: this action requires a real authenticated caller"
)


def _resolve_authz_actor(config: ServiceConfig) -> tuple[str, str] | None:
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


def _gated_actor(config: ServiceConfig) -> tuple[str, str]:
    """Return the verified `(sub, iss)` of a caller the tool's role gate has already admitted.

    Only for a tool declared as a `ROLE` gate that refuses the local-test bypass: the gate wrapper
    has already turned a missing identity into the unauthenticated message, so none is missing here.

    Args:
        config: Passed to `_resolve_authz_actor`.

    Returns:
        The verified `(sub, iss)` pair.
    """
    actor = _resolve_authz_actor(config)
    assert actor is not None  # noqa: S101  # the gate wrapper has already refused a caller with no identity
    return actor


def _resolve_policy_lifecycle_actor(config: ServiceConfig) -> tuple[str, str] | None:
    """Resolve the `(sub, iss)` identity for the policy-lifecycle tools.

    Same verified-bearer-token branch as `_resolve_authz_actor`, but it falls back to
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


def _audit_context(config: ServiceConfig, actor: tuple[str, str] | None) -> AuditContext | str:
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


def _resolve_base_url(ctx: Context) -> str:
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

_SYSTEM_ADMIN_GATE = ToolGate(
    kind=GateKind.ROLE,
    role=AccessRole.SYSTEM_ADMIN,
    bypass=BypassPolicy.SKIP,
    unauthenticated_message=_CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE,
)
_ACCESS_ROLE_MANAGEMENT_GATE = ToolGate(
    kind=GateKind.ROLE,
    role=AccessRole.SYSTEM_ADMIN,
    bypass=BypassPolicy.REFUSE,
    unauthenticated_message=_ACCESS_ROLE_MANAGEMENT_REQUIRES_AUTHENTICATED_CALLER_MESSAGE,
)
_CLEANUP_GATE = ToolGate(
    kind=GateKind.ROLE,
    role=AccessRole.COMPLIANCE_OFFICER,
    bypass=BypassPolicy.REFUSE,
    unauthenticated_message=_CLEANUP_REQUIRES_AUTHENTICATED_CALLER_MESSAGE,
)
_NEAR_MISS_MERGE_GATE = ToolGate(
    kind=GateKind.BODY,
    role=AccessRole.COMPLIANCE_OFFICER,
    bypass=BypassPolicy.REFUSE,
    unauthenticated_message=_CLEANUP_REQUIRES_AUTHENTICATED_CALLER_MESSAGE,
)
_COMPLIANCE_OFFICER_GATE = ToolGate(
    kind=GateKind.ROLE,
    role=AccessRole.COMPLIANCE_OFFICER,
    bypass=BypassPolicy.SKIP,
    unauthenticated_message=_CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE,
)
_PLAIN_READ_GATE = ToolGate(
    kind=GateKind.AUTHENTICATED_ONLY,
    reason="a plain read of the graph or the curated catalog; any authenticated caller",
)
_OWN_APPROVAL_READ_GATE = ToolGate(
    kind=GateKind.AUTHENTICATED_ONLY,
    reason="the tool reads only an approval the caller created; it needs a real session",
)
_ACCESS_ROLE_CHANGE_GATE = ToolGate(
    kind=GateKind.SELF_AUDITED,
    reason=(
        "the per-role actor requirement is enforced in grant_role/revoke_role, which write the "
        "access_role rejected row themselves"
    ),
)
_POLICY_AUTHOR_GATE = ToolGate(
    kind=GateKind.AUTHENTICATED_ONLY,
    reason="any authenticated caller may create a draft or act on a Policy they own",
)
_POLICY_DRAFT_ACCESS_GATE = ToolGate(
    kind=GateKind.AUTHENTICATED_ONLY,
    reason="the Policy owner, SystemOwner or SystemAdmin; enforced in the lifecycle service",
)


def _run_mcp_action(
    action: str,
    principal: str | None,
    body: Callable[[], dict[str, object] | str],
    *,
    entity_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, object] | str:
    """Run one MCP tool body in a run context with a uniform started/succeeded/failed log triad.

    The shared helper for every action-taking tool, so the log triad and the generic-exception
    safety net live in exactly one place. It binds a fresh run id, emits a
    `component="mcp_interface"` `outcome="started"` entry carrying `principal`, then runs `body`. A
    `body` that returns a string beginning `"error:"` is a handled failure: it is logged
    `outcome="failed"` with the (truncated) text as `reason` and returned unchanged. A `body` that
    raises `Exception` is the residual, not-yet-sanitised case: it is converted to the fixed generic
    error string, with the full `repr` logged server-side only. Any other return value is a success,
    logged `outcome="succeeded"` and returned unchanged.

    Args:
        action: The tool's own name (for example `"ingest_regulation"`), used as the log entries'
            `action` field.
        principal: The caller identity already resolved by `_resolve_principal`; recorded on every
            log entry (`"unknown"` when `None`).
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
    principal_extra = principal or "unknown"
    with bind_run_context(run_id) as bound_run_id:
        emit_log_entry(
            component=_COMPONENT,
            action=action,
            outcome="started",
            run_id=bound_run_id,
            entity_id=entity_id,
            extra={"principal": principal_extra},
        )
        try:
            result = body()
        except Exception as exc:  # noqa: BLE001 -- residual MCP-boundary safety net: body() must never raise across the MCP boundary
            emit_log_entry(
                component=_COMPONENT,
                action=action,
                outcome="failed",
                run_id=bound_run_id,
                entity_id=entity_id,
                extra={
                    "principal": principal_extra,
                    "exception_type": type(exc).__name__,
                    "detail": stage_failure_detail(exc),
                },
            )
            return _UNEXPECTED_ERROR_MESSAGE
        if isinstance(result, str) and result.startswith("error:"):
            emit_log_entry(
                component=_COMPONENT,
                action=action,
                outcome="failed",
                run_id=bound_run_id,
                entity_id=entity_id,
                extra={"principal": principal_extra, "reason": result[:_STAGE_REASON_MAX_LEN]},
            )
            return result
        emit_log_entry(
            component=_COMPONENT,
            action=action,
            outcome="succeeded",
            run_id=bound_run_id,
            entity_id=entity_id,
            extra={"principal": principal_extra},
        )
        return result


def _sanitize_graph_open[**P, R](opener: Callable[P, R]) -> Callable[P, R]:
    """Wrap one graph opener so any failure becomes `McpGraphUnavailableError`.

    Keeps host, port and driver detail from crossing the MCP boundary, as `_resolve_graph` does for
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


def _sanitize_cleanup_graph_open(
    dependencies: GraphCleanupDependencies,
) -> GraphCleanupDependencies:
    """Return `dependencies` with its graph opener wrapped by `_sanitize_graph_open`."""
    return dataclasses.replace(
        dependencies,
        open_single_tenant_graph=_sanitize_graph_open(dependencies.open_single_tenant_graph),
    )


def _sanitize_pipeline_graph_opens(dependencies: PipelineDependencies) -> PipelineDependencies:
    """Return `dependencies` with its three graph openers wrapped by `_sanitize_graph_open`.

    `run_catalog_ingestion_pipeline` calls the `native`, `baseline` and `single_tenant` openers
    directly, before any stage runs, so a stage failure's own sanitising never covers them. Without
    this wrapper an unreachable FalkorDB would reach the caller as the generic residual error of
    `_run_mcp_action`.
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
    """Return change-check `dependencies` with every graph opener wrapped by `_sanitize_graph_open`.

    `run_change_check_sweep` calls `open_single_tenant` directly, and the re-ingest path calls
    `open_native` the same way, neither with error handling of its own. Both are wrapped here, and
    the nested pipeline dependencies go through `_sanitize_pipeline_graph_opens`.
    """
    return dataclasses.replace(
        dependencies,
        open_single_tenant=_sanitize_graph_open(dependencies.open_single_tenant),
        open_native=_sanitize_graph_open(dependencies.open_native),
        pipeline=_sanitize_pipeline_graph_opens(dependencies.pipeline),
    )


def _sanitize_near_miss_review_graph_opens(
    dependencies: NearMissReviewDependencies,
) -> NearMissReviewDependencies:
    """Return `dependencies` with `open_single_tenant_graph` wrapped by `_sanitize_graph_open`.

    The near-miss list and resolve orchestration call that opener directly with no error handling of
    their own.
    """
    return dataclasses.replace(
        dependencies,
        open_single_tenant_graph=_sanitize_graph_open(dependencies.open_single_tenant_graph),
    )


def _sanitize_restore_graph_opens(
    dependencies: CatalogRestoreDependencies,
) -> CatalogRestoreDependencies:
    """Return restore `dependencies` with `open_db` wrapped by `_sanitize_graph_open`.

    `run_restoration_from_catalog_source` calls `open_db` directly with no error handling of its
    own. `single_tenant_graph_name` is a pure name lookup with no I/O and needs no wrapping.
    """
    return dataclasses.replace(dependencies, open_db=_sanitize_graph_open(dependencies.open_db))


@dataclasses.dataclass(frozen=True, slots=True)
class _PreparedCatalogIngestion:
    """A validated, resolved catalog-ingestion request, ready for the pipeline run.

    The hand-off between `_prepare_catalog_ingestion` (the fast, synchronous validation and
    resolution half of an ingestion) and `_run_prepared_catalog_ingestion` (the blocking
    pipeline-run half), so any tool can share the validation half without duplicating it.

    Attributes:
        entry: The catalog entry resolved for the request's CELEX and normalised short name.
        ingestion_adapter: Adapter bound to the document fetched during resolution, or `None` when
            resolution produced none.
        dependencies: The sanitised pipeline dependency bundle the single-tenant graph was opened
            from, reused unchanged for the run.
    """

    entry: CatalogEntry
    ingestion_adapter: IngestionAdapter | None
    dependencies: PipelineDependencies


# Domain errors that `_prepare_catalog_ingestion` and `_run_prepared_catalog_ingestion` map to
# `f"error: {exc}"`, shared by both halves so the map can never drift.
_CATALOG_INGESTION_ERRORS = (
    CatalogIdentifierNotFoundError,
    CelexAlreadyIngestedError,
    GraphWriteUnavailableError,
    IngestionConfigIncompleteError,
    PipelineStageError,
    ShortNameCollisionError,
)


def _prepare_catalog_ingestion(
    celex: str, short_name: str, *, config: ServiceConfig
) -> _PreparedCatalogIngestion | str:
    """Validate and resolve a catalog-ingestion request against the live graph, without running it.

    Calls `resolve_ingestion_entry`, the function `POST /ingestions` also calls, against the
    single-tenant graph opened from this call's sanitised dependency bundle. Identity is decided by
    the live graph and Cellar/ELI, never by the curated catalog: `CelexAlreadyIngestedError` when
    `celex` is already in the graph, `ShortNameCollisionError` when `short_name` is claimed by a
    different CELEX; otherwise `celex` is resolved against Cellar/ELI under the normalised
    `short_name`, used verbatim and never derived from the fetched title.

    Args:
        celex: CELEX identifier of the regulation.
        short_name: Caller-supplied short name, in any letter case.
        config: Resolved service configuration.

    Returns:
        The prepared request, or an `error: ` string for a mapped domain error (identifier not
        found, already ingested, short-name collision, incomplete configuration, graph write
        unavailable, pipeline stage failure) or the fixed graph-unavailable text.

    Raises:
        Exception: Any error not in the mapped set propagates to `_run_mcp_action`, which converts
            it to the generic error string.

    Side Effects:
        Opens graph connections and fetches the document from Cellar/ELI. Nothing is written.
    """
    try:
        dependencies = _sanitize_pipeline_graph_opens(build_default_pipeline_dependencies())
        single_tenant_graph = dependencies.graphs.single_tenant(config)
        resolution = resolve_ingestion_entry(
            celex, short_name, single_tenant_graph=single_tenant_graph
        )
    except _CATALOG_INGESTION_ERRORS as exc:
        return f"error: {exc}"
    except McpGraphUnavailableError:
        return _GRAPH_UNAVAILABLE_MESSAGE
    return _PreparedCatalogIngestion(
        entry=resolution.entry,
        ingestion_adapter=resolution.adapter,
        dependencies=dependencies,
    )


@dataclasses.dataclass
class _FailureCapture:
    """Carries the audit `reason_code` of the branch that failed a background run.

    `_run_mcp_action` returns only the `error:` text, which must never be parsed back into a reason,
    so the failing `except` arm records its classification here instead.

    Attributes:
        reason_code: Enumerated cause of the failure, or `None` until a failing branch sets it.
    """

    reason_code: IngestionReasonCode | None = None


def _run_prepared_catalog_ingestion(
    prepared: _PreparedCatalogIngestion,
    *,
    config: ServiceConfig,
    principal: str | None,
    run_id: str,
    trigger: IngestionTrigger,
    capture: _FailureCapture | None = None,
) -> dict[str, object] | str:
    """Run the pipeline for a prepared request and map its domain errors to `error:` strings.

    Uses the same error map as `_prepare_catalog_ingestion`, plus `AuditTrailUnavailableError`. The
    response shaping (`_to_accepted_response`, looked up as a module global at call time) stays
    outside the `try`.

    Args:
        prepared: The validated request from `_prepare_catalog_ingestion`.
        config: Resolved service configuration.
        principal: Caller identity; `None` is passed to the pipeline as the caller label
            `"unknown"`.
        run_id: Run id the pipeline and its log lines are keyed by.
        trigger: Audit trigger recorded for the run.
        capture: Optional collector; when given, a mapped failure stores its classified reason code
            on it.

    Returns:
        The accepted-response dict (`run_id`, instrument id, source and per-stage summaries), or an
        `error: ` string for a mapped domain error or the fixed graph-unavailable text.

    Raises:
        Exception: Any error not in the mapped set propagates to `_run_mcp_action`.

    Side Effects:
        Runs the ingestion pipeline: writes the compliance graph, calls Cellar/ELI and the LLM
        Interface, and emits log entries. Sets `capture.reason_code` on a mapped failure.
    """
    try:
        outcome = run_catalog_ingestion_pipeline(
            prepared.entry,
            config=config,
            run_id=run_id,
            caller=principal or "unknown",
            dependencies=prepared.dependencies,
            ingestion_adapter=prepared.ingestion_adapter,
            trigger=trigger,
        )
    except (*_CATALOG_INGESTION_ERRORS, AuditTrailUnavailableError) as exc:
        if capture is not None:
            capture.reason_code = classify_ingestion_failure(exc)
        return f"error: {exc}"
    except McpGraphUnavailableError as exc:
        if capture is not None:
            capture.reason_code = classify_ingestion_failure(exc)
        return _GRAPH_UNAVAILABLE_MESSAGE
    return _to_accepted_response(run_id, outcome).model_dump()


def _resolve_and_ingest(
    celex: str,
    short_name: str,
    *,
    config: ServiceConfig,
    principal: str | None,
    run_id: str,
    audit: AuditContext,
) -> dict[str, object] | str:
    """Run the audited synchronous ingestion (identity check plus pipeline) and map its errors.

    Delegates to `run_audited_catalog_ingestion`, the orchestration `POST /ingestions` also calls.
    It writes the `ingestion_run.submit` opening row first (fail-closed: an unavailable audit trail
    yields the fixed `error: ` text and nothing runs), resolves the request's identity, runs the
    pipeline and writes the terminal row. The sanitised dependency bundle makes a graph failure map
    to `_GRAPH_UNAVAILABLE_MESSAGE`. Response shaping stays outside the `try`.

    Args:
        celex: CELEX identifier of the regulation.
        short_name: Caller-supplied short name, in any letter case.
        config: Resolved service configuration.
        principal: Caller identity; `None` is passed on as the caller label `"unknown"`.
        run_id: Run id the rows and log lines are keyed by.
        audit: Actor and audit store for the two audit rows.

    Returns:
        The accepted-response dict, or an `error: ` string for a mapped domain error, an unavailable
        audit trail or the fixed graph-unavailable text.

    Raises:
        Exception: Any error not in the mapped set propagates to `_run_mcp_action`.

    Side Effects:
        Writes the `ingestion_run.submit` and `ingestion_run.complete` audit rows and the pipeline's
        graph writes.
    """
    try:
        outcome = run_audited_catalog_ingestion(
            celex,
            short_name,
            config=config,
            run_id=run_id,
            caller=principal or "unknown",
            dependencies=_sanitize_pipeline_graph_opens(build_default_pipeline_dependencies()),
            audit=audit,
        )
    except (*_CATALOG_INGESTION_ERRORS, AuditTrailUnavailableError) as exc:
        return f"error: {exc}"
    except McpGraphUnavailableError:
        return _GRAPH_UNAVAILABLE_MESSAGE
    return _to_accepted_response(run_id, outcome).model_dump()


@_gated_tool("ingest_regulation", _COMPLIANCE_OFFICER_GATE)
def ingest_regulation(
    celex: Annotated[str, Field(min_length=10, max_length=10, pattern=_CELEX_PATTERN)],
    short_name: Annotated[str, Field(pattern=_SHORT_NAME_PATTERN)],
) -> dict[str, object] | str:
    """Ingest one EU regulation by CELEX identifier into the compliance graph and wait for it.

    Runs the full pipeline (ingestion, extraction, derivation, merge) for `celex` and blocks until
    it finishes, which can take several minutes; use `start_ingestion` to return at once and poll
    instead. Any valid CELEX is resolved against Cellar/ELI and ingested under the `short_name` you
    give, used verbatim and never derived from the fetched title, so the same CELEX never forks into
    two differently named graphs. The ingested instrument is `<SHORT_NAME>-<version>`. Requires the
    ComplianceOfficer role (skipped only under the local-test bypass).

    Args:
        celex: CELEX identifier of the regulation: exactly 10 characters, a `3`, a four-digit year,
            one capital letter and four digits (for example `32024R2847`).
        short_name: Your short name for the instrument: a letter first, then letters, digits, `_` or
            `-`, 1-64 characters in all. Normalised to upper case, so `cra` and `CRA` are the same
            name.

    Returns:
        A summary dict on success: `run_id`, `regulatory_instrument_id`, `source`, and one `stages`
        entry per completed pipeline stage (ingestion, extraction, derivation, merge), each with a
        small integer `summary`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller is not authenticated or lacks the ComplianceOfficer role.
        - `celex` is already ingested (under any `short_name`), or `short_name` is already claimed,
          in any letter case, by a different CELEX.
        - `celex` does not exist on Cellar/ELI.
        - The language-model service is currently unavailable (checked before any graph is opened).
        - The service configuration is missing an LLM or embedding model or the similarity
          threshold.
        - The policy graph database cannot be reached, or a pipeline stage fails mid-run.
        - The audit trail is temporarily unavailable (nothing is run).
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the instrument's nodes and edges to the compliance graph and calls Cellar/ELI and the
        LLM Interface. Every accepted call is audited: an `ingestion_run.submit` row
        (`trigger='sync_ingest'`) is written before anything runs, and one `ingestion_run.complete`
        row records the instrument id and the counts of new Obligations, new Capabilities and
        matched Capabilities, or an enumerated failure `reason_code`. An already-ingested CELEX is
        audited too.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        if not dependency_health.is_healthy(dependency_health.LLM_INTERFACE):
            return _LLM_INTERFACE_UNAVAILABLE_MESSAGE
        # `_run_mcp_action` always binds a run_id via `bind_run_context()` before
        # calling this closure, so `current_run_id()` is never actually `None`
        # here -- the `""` fallback only satisfies the type checker's narrowing,
        # mirroring `_resolve_principal`'s own "unreachable in practice" idiom.
        run_id = current_run_id() or ""
        audit = _audit_context(config, actor)
        if isinstance(audit, str):
            return audit
        return _resolve_and_ingest(
            celex, short_name, config=config, principal=principal, run_id=run_id, audit=audit
        )

    return _run_mcp_action("ingest_regulation", principal, _body)


_BACKGROUND_INGESTION_ACTION = "background_ingestion_run"
_RECONCILE_ACTION = "ingestion_run_reconcile"
_RECONCILER_AUDIT_ACTOR = (INGESTION_RUN_RECONCILER_ACTOR, INGESTION_RUN_RECONCILER_ACTOR)
"""Audit actor of a run completed by a status poll rather than by its own worker."""
_INTERRUPTED_RUN_MESSAGE = (
    "error: the ingestion run was interrupted before it finished; its outcome is unknown"
)


def _emit_best_effort_failure(action: str, run_id: str, **extra: object) -> None:
    """Log one `mcp_interface` `failed` entry for `run_id`; never raise.

    For failures on a worker thread, where nobody can be told: a process with no configured
    emitter must still finish recording the run's outcome.
    """
    with contextlib.suppress(LoggingLifecycleError):
        emit_log_entry(
            component=_COMPONENT,
            action=action,
            outcome="failed",
            run_id=run_id,
            extra=dict(extra),
        )


def _record_ingestion_run_outcome(
    store: IngestionRunStore,
    *,
    run_id: str,
    result: dict[str, object] | str,
    reason_code: IngestionReasonCode | None,
) -> None:
    """Write the run's one terminal row: `failed` for an `error:` string, else `succeeded`.

    `reason_code` is the enumerated cause of a failure (`unexpected_error` when no known branch
    recorded one); the `error:` text goes to the run row only, never to the audit row.

    The write is a single-winner compare-and-swap, so a row is never terminal twice.
    A failed write is logged (exception class only, never host detail) and swallowed: nothing
    may escape the worker unlogged, and the row simply stays `running`.
    """
    try:
        if isinstance(result, str):
            store.complete_run(
                run_id,
                status="failed",
                result=None,
                error=result,
                reason_code=reason_code or "unexpected_error",
            )
        else:
            store.complete_run(run_id, status="succeeded", result=result, error=None)
    except Exception as exc:  # noqa: BLE001 -- nothing may escape the worker unlogged; the row stays `running`
        _emit_best_effort_failure(_BACKGROUND_INGESTION_ACTION, run_id, reason=type(exc).__name__)


def _execute_background_ingestion(
    prepared: _PreparedCatalogIngestion,
    *,
    run_id: str,
    config: ServiceConfig,
    principal: str | None,
    store: IngestionRunStore,
) -> None:
    """Run the prepared pipeline on the worker thread, then record its outcome.

    The pipeline call goes through `_run_mcp_action` so the run gets its own correlated
    started/succeeded/failed log lines under the submitted `run_id` and every unexpected
    exception is sanitized exactly as for a blocking tool call. The outer `except` is the
    last-resort net: the run's row must never be left `running` by an exception.
    """
    capture = _FailureCapture()
    try:
        result = _run_mcp_action(
            _BACKGROUND_INGESTION_ACTION,
            principal,
            lambda: _run_prepared_catalog_ingestion(
                prepared,
                config=config,
                principal=principal,
                run_id=run_id,
                trigger="async_ingest",
                capture=capture,
            ),
            run_id=run_id,
        )
    except Exception as exc:  # noqa: BLE001 -- last-resort net: the row must never stay 'running'
        _emit_best_effort_failure(
            _BACKGROUND_INGESTION_ACTION,
            run_id,
            principal=principal or "unknown",
            exception_type=type(exc).__name__,
            detail=stage_failure_detail(exc),
        )
        result = _UNEXPECTED_ERROR_MESSAGE
    _record_ingestion_run_outcome(
        store, run_id=run_id, result=result, reason_code=capture.reason_code
    )


def _reconcile_orphaned_run(store: IngestionRunStore, row: IngestionRunRow) -> IngestionRunRow:
    """Mark a `running` row no in-process worker holds as failed (interrupted), then re-read it.

    A declared, idempotent side effect of `get_ingestion_status`: the row is
    `running` yet its worker is gone (its terminal write failed, or the process restarted), so
    it would otherwise be reported `running` forever. The write is a single-winner
    compare-and-swap, a no-op if the worker finished first. It never fails the poll: any store
    error is logged (exception class only) and the row is returned unchanged.
    """
    if row.status != "running" or is_run_in_flight(row.run_id):
        return row
    try:
        store.complete_run(
            row.run_id,
            status="failed",
            result=None,
            error=_INTERRUPTED_RUN_MESSAGE,
            reason_code="interrupted",
            audit_actor=_RECONCILER_AUDIT_ACTOR,
        )
        reread = store.get_run(row.run_id)
    except Exception as exc:  # noqa: BLE001 -- a failed reconciliation must never become the poll's failure
        _emit_best_effort_failure(_RECONCILE_ACTION, row.run_id, reason=type(exc).__name__)
        return row
    return reread if reread is not None else row


def _check_ingestion_config(config: ServiceConfig) -> str | None:
    """Return the `error:` text when the pipeline config is incomplete, else `None`.

    `start_ingestion` runs this synchronously so an incomplete configuration is reported to the
    caller exactly as `ingest_regulation` reports it (the pipeline's own first step), instead of
    surfacing only later as a failed background run.
    """
    try:
        _require_ingestion_config(config)
    except IngestionConfigIncompleteError as exc:
        return f"error: {exc}"
    return None


def _submit_ingestion_run(
    prepared: _PreparedCatalogIngestion,
    *,
    run_id: str,
    config: ServiceConfig,
    principal: str | None,
    actor: tuple[str, str] | None,
    store: IngestionRunStore,
) -> dict[str, object] | str:
    """Record the run, start its worker thread and return at once.

    The slot is reserved atomically (duplicate `short_name` and the in-flight cap are checked
    before anything is written) and released on any failure before the worker starts, so no
    exception can leak a slot.
    """
    audit = _audit_context(config, actor)
    if isinstance(audit, str):
        return audit
    try:
        reserve_run_slot(
            run_id,
            short_name=prepared.entry.short_name,
            max_in_flight_runs=config.ingestion_runs_max_in_flight,
        )
    except (IngestionRunCapacityExceededError, IngestionRunAlreadyInProgressError) as exc:
        return f"error: {exc}"
    try:
        store.create_run(
            run_id=run_id,
            celex=prepared.entry.celex,
            short_name=prepared.entry.short_name,
            actor=audit.actor,
        )
        start_background_run(
            run_id,
            functools.partial(
                _execute_background_ingestion,
                prepared,
                run_id=run_id,
                config=config,
                principal=principal,
                store=store,
            ),
        )
    except IngestionRunStoreError as exc:
        release_run_slot(run_id)
        return f"error: {exc}"
    except BaseException:
        release_run_slot(run_id)  # no leaked slot on any non-store error; a no-op if already freed
        raise
    return StartIngestionResponse(run_id=run_id, status="running").model_dump()


def _ingestion_run_status_response(run_id: str, row: IngestionRunRow | None) -> dict[str, object]:
    """Shape one run row (or its absence) as the single five-key `get_ingestion_status` payload."""
    if row is None:
        return IngestionRunStatusResponse(
            run_id=run_id, status="unknown", stage=None, result=None, error=None
        ).model_dump()
    return IngestionRunStatusResponse(
        run_id=row.run_id,
        status=row.status,
        stage=get_stage(row.run_id) if row.status == "running" else None,
        result=row.result,
        error=row.error,
    ).model_dump()


@_gated_tool("start_ingestion", _COMPLIANCE_OFFICER_GATE)
def start_ingestion(
    celex: Annotated[str, Field(min_length=10, max_length=10, pattern=_CELEX_PATTERN)],
    short_name: Annotated[str, Field(pattern=_SHORT_NAME_PATTERN)],
) -> dict[str, object] | str:
    """Start ingesting one EU regulation by CELEX identifier and return at once.

    Takes the inputs of `ingest_regulation` and runs the same fast pre-flight checks synchronously,
    but does not wait for the multi-minute pipeline (ingestion, extraction, derivation, merge): it
    records the run, starts it on a background thread and returns immediately. Poll
    `get_ingestion_status` with the returned `run_id` for the live stage and the final result.
    Requires the ComplianceOfficer role (skipped only under the local-test bypass).

    Args:
        celex: CELEX identifier of the regulation: exactly 10 characters, a `3`, a four-digit year,
            one capital letter and four digits (for example `32024R2847`).
        short_name: Your short name for the instrument: a letter first, then letters, digits, `_` or
            `-`, 1-64 characters in all. Normalised to upper case, so `cra` and `CRA` are the same
            name.

    Returns:
        `{"run_id": <id>, "status": "running"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller is not authenticated or lacks the ComplianceOfficer role.
        - Any pre-flight failure `ingest_regulation` reports: `celex` already ingested, `short_name`
          claimed by a different CELEX, `celex` not found on Cellar/ELI, LLM Interface unhealthy,
          the service configuration incomplete, or the policy graph database unreachable.
        - A run for the same `short_name` is already in progress, or too many runs are in flight.
        - The run could not be recorded, or the run store is unavailable.
        - A failure inside the pipeline itself is not reported by this call; `get_ingestion_status`
          reports it later.

    Side Effects:
        Creates an ingestion-run record, reserves a run slot and starts a background thread. The
        thread then runs the pipeline exactly as `ingest_regulation` does (graph writes,
        `trigger='async_ingest'` audit rows) and records the run's terminal outcome.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        if not dependency_health.is_healthy(dependency_health.LLM_INTERFACE):
            return _LLM_INTERFACE_UNAVAILABLE_MESSAGE
        prepared = _prepare_catalog_ingestion(celex, short_name, config=config)
        if isinstance(prepared, str):
            return prepared
        incomplete = _check_ingestion_config(config)
        if incomplete is not None:
            return incomplete
        # `_run_mcp_action` always binds a run_id before calling this closure, so
        # `current_run_id()` is never `None` here -- the `""` fallback only satisfies the
        # type checker, as in `ingest_regulation`. This is the id the row, the live-stage
        # registry, the pipeline's log lines and the worker's log lines all share.
        run_id = current_run_id() or ""
        return _submit_ingestion_run(
            prepared,
            run_id=run_id,
            config=config,
            principal=principal,
            actor=actor,
            store=PsycopgIngestionRunStore(config, audit_store=PsycopgAuditStore(config)),
        )

    return _run_mcp_action("start_ingestion", principal, _body)


@_gated_tool("get_ingestion_status", _COMPLIANCE_OFFICER_GATE)
def get_ingestion_status(
    run_id: Annotated[str, Field(min_length=1, max_length=64)],
) -> dict[str, object] | str:
    """Report the progress and final result of a `start_ingestion` run.

    Requires the ComplianceOfficer role (skipped only under the local-test bypass), checked before
    any lookup.

    Args:
        run_id: The `run_id` returned by `start_ingestion`, 1-64 characters.

    Returns:
        One fixed shape for every outcome: `run_id`; `status` (`running`, `succeeded`, `failed`, or
        `unknown` for an id that was never submitted); `stage` (the stage a running run is
        executing, when known, else `None`); `result` (on success, the same summary
        `ingest_regulation` returns); and `error` (on failure, the `error: ...` text
        `ingest_regulation` would have returned).

    Errors (returned as a string beginning `error: `, never raised):
        - The caller is not authenticated or lacks the ComplianceOfficer role.
        - The run store is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Read-only except for one idempotent repair: a run recorded as `running` that no worker in
        the service holds (its terminal write failed, or the process restarted) is marked `failed`
        as interrupted, with its completion audit row, so it is not reported `running` forever.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        store = PsycopgIngestionRunStore(config, audit_store=PsycopgAuditStore(config))
        try:
            row = store.get_run(run_id)
        except IngestionRunStoreError as exc:
            return f"error: {exc}"
        if row is not None:
            row = _reconcile_orphaned_run(store, row)
        return _ingestion_run_status_response(run_id, row)

    return _run_mcp_action("get_ingestion_status", principal, _body)


def _run_audited_change_check(
    config: ServiceConfig, actor: tuple[str, str] | None, run_id: str
) -> dict[str, object] | str:
    """Run the `check_regulations` sweep with the caller as audit actor.

    Args:
        config: Resolved service configuration.
        actor: Verified `(sub, iss)` of the caller, or `None` when none was bound.
        run_id: Run id of the sweep.

    Returns:
        The change-check response dict, or an `error: ` string: the fixed authentication text when
        there is no actor and no bypass, the fixed graph-unavailable text, or the message of an
        unavailable audit trail or compliance graph write.

    Raises:
        Exception: Any other error propagates to `_run_mcp_action`.

    Side Effects:
        The sweep polls each tracked instrument's source and re-ingests detected amendments, writing
        the graph and, per re-ingest, `ingestion_run.submit` and `ingestion_run.complete` audit
        rows.
    """
    audit = _audit_context(config, actor)
    if isinstance(audit, str):
        return audit
    try:
        result = run_change_check_sweep(
            config=config,
            run_id=run_id,
            dependencies=_sanitize_change_check_graph_opens(
                build_default_change_check_dependencies()
            ),
            audit=audit,
        )
    except McpGraphUnavailableError:
        return _GRAPH_UNAVAILABLE_MESSAGE
    except (AuditTrailUnavailableError, GraphWriteUnavailableError) as exc:
        return f"error: {exc}"
    return _to_change_check_response(result).model_dump()


@_gated_tool("check_regulations", _COMPLIANCE_OFFICER_GATE)
def check_regulations() -> dict[str, object] | str:
    """Sweep every tracked regulation for detected amendments and re-ingest any found.

    Opens the merged compliance graph, reads every actively tracked external `regulation` or
    `directive` instrument, polls for a newer consolidated version of each, and automatically
    re-ingests any detected amendment. Takes no parameters. Requires the ComplianceOfficer role
    (skipped only under the local-test bypass).

    Returns:
        A summary dict on success: `run_id` and one `instruments` entry per tracked instrument, each
        with its `instrument_id` and an `outcome` (`current`, `amendment_reingested`, `poll_failed`,
        `not_configured`, `skipped` or `reingest_failed`), plus `detail` and `reingest_run_id` where
        applicable.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller is not authenticated or lacks the ComplianceOfficer role.
        - The language-model service is currently unavailable (checked before any graph is opened).
        - The policy graph database cannot be reached.
        - The audit trail cannot record a re-ingest. The sweep stops before that re-ingest; earlier
          re-ingests of the same sweep stay recorded.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Every re-ingest the sweep runs writes to the compliance graph and to the audit trail: an
        `ingestion_run.submit` row (`trigger='amendment_check'`, written before the re-ingest) and
        an `ingestion_run.complete` row (instrument id and the real Obligation and Capability counts
        from the merge result, or the failure `reason_code`), keyed by the re-ingest's own run id
        (the `reingest_run_id` in the result) and attributed to the caller. Read them with
        `list-audit-events`.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        if not dependency_health.is_healthy(dependency_health.LLM_INTERFACE):
            return _LLM_INTERFACE_UNAVAILABLE_MESSAGE
        # `_run_mcp_action` always binds a run_id via `bind_run_context()` before
        # calling this closure, so `current_run_id()` is never actually `None`
        # here -- the `""` fallback only satisfies the type checker's narrowing,
        # mirroring `ingest_regulation`'s own identical idiom.
        return _run_audited_change_check(config, actor, current_run_id() or "")

    return _run_mcp_action("check_regulations", principal, _body)


@_gated_tool("near_misses_list", _PLAIN_READ_GATE)
def near_misses_list() -> dict[str, object] | str:
    """List every unresolved near-miss pending review.

    A near-miss is a Company Merge deduplication candidate awaiting a keep-separate or merge
    decision. Reads the merged compliance graph. Takes no parameters and needs no LLM Interface,
    because deduplication runs only during ingestion and merge, never during this read.

    Returns:
        A dict with a `reviews` list (empty when none are unresolved), one entry per unresolved
        review with `id`, `kind`, `incoming_text`, `nearest_existing_text` and `similarity`
        (0.0-1.0). The two canonical node ids a review references are deliberately omitted.

    Errors (returned as a string beginning `error: `, never raised):
        - The policy graph database cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        None on the graph. The call is logged.
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


def _resolve_merge_pending_approval(
    review_id: str, ctx: Context, config: ServiceConfig, *, acknowledge: bool
) -> dict[str, object] | str:
    """Handle `near_misses_resolve` with `decision="merge"` by requesting a cleanup merge approval.

    Split out of the tool body to keep its branch count manageable; no other tool uses it.

    Args:
        review_id: Id of the near-miss pending review to merge.
        ctx: MCP request context, used to derive the approval link's base URL.
        config: Resolved service configuration.
        acknowledge: True when the caller accepts the governance change of a case-2 merge.

    Returns:
        `{"preview", "pending_approval_id", "approval_url", "expires_at"}`; for case 2 without the
        acknowledgment `{"preview", "acknowledgment_required": True, "message"}` and no approval; or
        an `error: ` string when the caller is not an authenticated Compliance Officer, the review
        does not exist or is stale or is not a Capability review, the pair is governed by different
        Policies, or the graph is unreachable.

    Raises:
        Exception: Any other error propagates to `_run_mcp_action`.

    Side Effects:
        Inserts one pending-approval row on success. The merge itself runs only after a human signs.
        Nothing is written to the graph.
    """
    actor = run_body_gate(_gate_environment, "near_misses_resolve")
    if isinstance(actor, str):
        return actor
    try:
        approval = run_request_near_miss_merge(
            review_id,
            actor=actor,
            acknowledge_governance_change=acknowledge,
            base_url=_resolve_base_url(ctx),
            config=config,
            dependencies=_sanitize_near_miss_review_graph_opens(
                build_default_near_miss_review_dependencies()
            ),
            store=PsycopgPendingApprovalStore(config),
        )
    except GraphCleanupAcknowledgmentRequiredError as exc:
        return {
            "preview": exc.preview.model_dump(mode="json"),
            "acknowledgment_required": True,
            "message": str(exc),
        }
    except (PendingReviewNotFoundError, GraphCleanupValidationError) as exc:
        return f"error: {exc}"
    except McpGraphUnavailableError, GraphCleanupPersistenceError:
        return _GRAPH_UNAVAILABLE_MESSAGE
    return {
        "preview": approval.preview.model_dump(mode="json"),
        "pending_approval_id": approval.pending_approval_id,
        "approval_url": approval.approval_url,
        "expires_at": approval.expires_at,
    }


# The merge-only fields of `ResolveReviewResponse`; a keep-separate result never carries them.
_MERGE_ONLY_RESOLVE_FIELDS = {"preview", "acknowledgment_required", "message"}


@_gated_tool("near_misses_resolve", _NEAR_MISS_MERGE_GATE)
def near_misses_resolve(
    review_id: Annotated[str, Field(min_length=1)],
    decision: Literal["keep-separate", "merge"],
    ctx: Context,
    *,
    acknowledge_governance_change: bool = False,
) -> dict[str, object] | str:
    """Resolve one near-miss pending review by keeping the two entities separate or merging them.

    `decision="keep-separate"` deletes only the `PendingReview` record and leaves both entities
    untouched. `decision="merge"` merges the two Capabilities through the graph-cleanup
    `merge-capabilities` approval and requires an explicit `ComplianceOfficer` grant on a real
    authenticated session (no admin override, never available under the local-test bypass). This
    call never edits the graph: it picks the survivor (the Capability with the earlier
    `created_at`; a missing one counts earliest and a tie goes to the incoming node), returns the
    merge preview and creates a pending passkey approval bound to that pair and state, with the
    review id signed into it. The merge runs only when the officer opens `approval_url` in a browser
    and signs with a passkey; use `near_misses_check_approval` to learn whether that happened.

    On execution the absorbed Capability stays as a tombstone (`status` `merged`, one `MERGED_INTO`
    edge to the survivor), so a later ingest or restore resolves to the survivor and raises no new
    near miss. Its `REQUIRES`, `COVERS` and `MITIGATED_BY` edges move to the survivor. The change is
    one all-or-nothing graph write, audited first (`capability.merge`, linked to the review id and
    reversible with `unmerge`), that also deletes the `PendingReview`. A Capability keeps at most
    one governing Policy: when exactly one of the two is governed the preview names the Policy
    (`policy_case` 2) and the first call, without `acknowledge_governance_change`, returns
    `acknowledgment_required` and creates no approval; repeat it with the flag set once the officer
    has accepted the change. Two Capabilities governed by different Policies are refused before any
    approval, with an error naming `release-capability-governance`. Needs no LLM Interface.

    Args:
        review_id: Id of the pending review, from `near_misses_list`; at least 1 character.
        decision: `"keep-separate"` or `"merge"`; any other value is rejected before the tool runs.
        ctx: Supplied by the MCP runtime, not by the caller.
        acknowledge_governance_change: `true` to accept the governance change of a case-2 merge;
            default `false`. It has no effect on `keep-separate` or on the other merge cases.

    Returns:
        For `keep-separate`: `{"review_id", "decision", "winner_id": None, "loser_id": None,
        "pending_approval_id": None, "approval_url": None, "expires_at": None}`. For `merge`:
        `{"preview": {"survivor_id", "survivor_name", "absorbed_id", "absorbed_name", "policy_case",
        "edges_to_move": {"requires", "covers", "mitigated_by"}, "duplicate_edges_collapsed",
        "obligations_affected", "state_digest", "governance"}, "pending_approval_id",
        "approval_url", "expires_at"}`, where `approval_url` is the link a human opens to sign. For
        a case-2 merge without the acknowledgment: `{"preview", "acknowledgment_required": true,
        "message"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - `review_id` does not exist, was already resolved, or no longer matches two live
          Capabilities (stale).
        - (`merge` only) The caller is not an authenticated Compliance Officer.
        - (`merge` only) The review is not a Capability review, or the two Capabilities are
          governed by different Policies.
        - (`keep-separate` only) The audit trail cannot record the resolve; the review is left
          untouched.
        - The policy graph database cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        `keep-separate` writes a `near_miss.resolve` audit row, then deletes the `PendingReview`
        record. `merge` inserts one pending-approval record and writes nothing to the graph; the
        merge is audited when its approval is signed.
        A `merge` caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        if decision == "merge":
            return _resolve_merge_pending_approval(
                review_id, ctx, config, acknowledge=acknowledge_governance_change
            )
        audit = _audit_context(config, actor)
        if isinstance(audit, str):
            return audit
        try:
            result = run_resolve_near_miss(
                review_id,
                decision,
                config=config,
                dependencies=_sanitize_near_miss_review_graph_opens(
                    build_default_near_miss_review_dependencies()
                ),
                audit=audit,
            )
        except (PendingReviewNotFoundError, AuditTrailUnavailableError) as exc:
            return f"error: {exc}"
        except McpGraphUnavailableError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return result.model_dump(exclude=_MERGE_ONLY_RESOLVE_FIELDS)

    return _run_mcp_action("near_misses_resolve", principal, _body)


@_gated_tool("near_misses_check_approval", _OWN_APPROVAL_READ_GATE)
def near_misses_check_approval(
    pending_approval_id: Annotated[str, Field(min_length=1)],
) -> dict[str, object] | str:
    """Check whether a near-miss merge's pending approval has been signed and what it did.

    `near_misses_resolve` with `decision="merge"` never waits for a human to complete the WebAuthn
    signing ceremony, so this is the separate, poll-again-later way to learn whether it has
    happened. The approval is a graph-cleanup `merge-capabilities` approval that carries the review
    id; any other approval is reported as not found. Ownership check: only the caller who created
    the approval may check it. A caller with no real authenticated session, or whose identity
    differs from the approval's, gets the same generic not-found error as for a wrong id, so a
    caller cannot learn whether an id belongs to someone else.

    Args:
        pending_approval_id: The `pending_approval_id` returned by `near_misses_resolve`; at least 1
            character.

    Returns:
        `{"pending_approval_id", "status", "review_id", "decision", "winner_id", "loser_id",
        "error"}`. `status` is `"pending"`, `"expired"` (the signing window elapsed unsigned;
        derived live, never stored) or `"signed"`. `winner_id` (the surviving Capability) and
        `loser_id` (the absorbed one, kept as a `merged` tombstone) are populated once the signed
        merge has run, and `error` when the signed approval could not be carried out; otherwise
        they are `None`.

    Errors (returned as a string beginning `error: `, never raised):
        - No such pending approval is visible to this caller.
        - The approval status could not be settled right now (the graph or audit trail is
          unreachable).
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        A signed approval whose outcome was never recorded is settled against the graph once it is
        more than five minutes past its expiry, as `check-cleanup-approval` does. Otherwise the call
        is read-only and logged.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        actor = _resolve_signing_actor()
        try:
            status = check_near_miss_merge_approval(
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
            "review_id": status.review_id,
            "decision": status.decision,
            "winner_id": status.winner_id,
            "loser_id": status.loser_id,
            "error": status.error,
        }

    return _run_mcp_action("near_misses_check_approval", principal, _body)


# Fixed, detail-free string for the catalog-source tools' read failures: never host, port,
# driver text or the (possibly credential-bearing) override URL.
_RUNTIME_CONFIG_UNAVAILABLE_MESSAGE = (
    "error: The runtime configuration store is temporarily unavailable."
)


@_gated_tool("set-catalog-source", _SYSTEM_ADMIN_GATE)
def set_catalog_source(url: Annotated[str, Field(min_length=1)]) -> dict[str, object] | str:
    """Override the curated-content source used for catalog listings and artifact fetches.

    Validates `url` with the same rules startup configuration applies: only `http(s)://` schemes are
    accepted, and a plain `http://` URL is rejected unless `PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP` is
    set for the service process. The override takes effect at once, with no restart, and takes
    precedence over `PS_CURATEDSOURCE_URL` and the public default for every later catalog listing
    and artifact fetch until `reset-catalog-source` is called. Requires the SystemAdmin role or
    above (skipped only under the local-test bypass).

    Args:
        url: Base URL of the curated-content source; at least 1 character, `http(s)://` only.

    Returns:
        `{"url": <the validated url>, "source": "override"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller is not authenticated or lacks the required access role.
        - `url` fails validation (the message says why).
        - The runtime configuration store cannot be reached or the write could not be applied;
          nothing is changed.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Persists `url` as the override in the PS state database and writes one audit event for the
        change in the same transaction.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        audit = _audit_context(config, actor)
        if isinstance(audit, str):
            return audit
        try:
            catalog_source_store.set_override(
                PsycopgRuntimeConfigStore(config, audit_store=audit.store),
                url,
                actor=audit.actor,
            )
        except (
            RuntimeConfigInvalidValueError,
            RuntimeConfigUnavailableError,
            RuntimeConfigPersistenceError,
        ) as exc:
            # Either the validator's own message or a fixed store message.
            return f"error: {exc}"
        return {"url": url, "source": "override"}

    return _run_mcp_action("set_catalog_source", principal, _body)


@_gated_tool("reset-catalog-source", _SYSTEM_ADMIN_GATE)
def reset_catalog_source() -> dict[str, object] | str:
    """Clear the persisted curated-content source override.

    The effective source immediately reverts to `PS_CURATEDSOURCE_URL` or the public default for
    every later catalog listing and artifact fetch, with no restart. Takes no parameters. Requires
    the SystemAdmin role or above (skipped only under the local-test bypass).

    Returns:
        `{"url": <the configured default url>, "source": "default"}`. Resetting when no override is
        set changes nothing and is not an error.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller is not authenticated or lacks the required access role.
        - The runtime configuration store cannot be reached or the write could not be applied;
          nothing is changed.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Deletes the persisted override from the PS state database, if any, and writes one audit
        event in the same transaction.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        audit = _audit_context(config, actor)
        if isinstance(audit, str):
            return audit
        try:
            catalog_source_store.reset_override(
                PsycopgRuntimeConfigStore(config, audit_store=audit.store),
                actor=audit.actor,
            )
        except (RuntimeConfigUnavailableError, RuntimeConfigPersistenceError) as exc:
            return f"error: {exc}"
        return {"url": config.curated_source_base_url, "source": "default"}

    return _run_mcp_action("reset_catalog_source", principal, _body)


@_gated_tool("get-catalog-source", _SYSTEM_ADMIN_GATE)
def get_catalog_source() -> dict[str, object] | str:
    """Report the currently effective curated-content source.

    Looks for a persisted override in the PS state database first and falls back to
    `PS_CURATEDSOURCE_URL` or the public default only when the store answered and holds no override.
    It fails closed: when the store cannot be read this tool returns an error and never reports the
    default source, because the operator's override may point elsewhere on purpose. Takes no
    parameters. Requires the SystemAdmin role or above (skipped only under the local-test bypass).

    Returns:
        `{"url": <the effective url>, "source": "override"}` when a persisted override is in effect,
        otherwise `{"url": <the default url>, "source": "default"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller is not authenticated or lacks the required access role.
        - The override cannot be read (the runtime configuration store is unavailable).
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        None; the call is read-only and logged.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
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
    """Log the `invitations` / `invite_user` `created` entry that names the invitee's email.

    `_run_mcp_action` already emits the generic started/succeeded/failed triad for every tool call,
    carrying the principal and a timestamp, but its fixed `extra` shape does not carry the target
    email. This is an additional emission alongside that triad, in the same shape as the
    `SystemOwner` floor warning logged by `ps_service.authz.service`. `invite_user` calls it only on
    the success path.

    Business Rules:
        - `ps_service.logging.models` tells callers never to put PII in `extra`. Putting the
          invitee's email in `entity_id` is a deliberate, scoped exception: an invite record with no
          target identity would be useless.
        - `LoggingLifecycleError` is swallowed, because a missing log sink must never turn a
          successful invite into a failed tool call.

    Args:
        actor: The verified `(sub, iss)` pair resolved for this call, or `None` under the bypass.
        principal: The resolved principal, used as the actor label when `actor` is `None`.
        email: The invitee's email address, recorded as the entry's `entity_id`.

    Side Effects:
        Emits one log entry.
    """
    with contextlib.suppress(LoggingLifecycleError):
        emit_log_entry(
            component="invitations",
            action="invite_user",
            entity_id=email,
            outcome="created",
            extra={"actor": actor[0] if actor is not None else (principal or "unknown")},
        )


@_gated_tool("invite-user", _SYSTEM_ADMIN_GATE)
def invite_user(
    email: Annotated[str, Field(min_length=3, pattern=_INVITE_EMAIL_PATTERN)],
) -> dict[str, object] | str:
    """Create a single-use Authentik enrollment invite for an email address.

    Uses the service's own configured Authentik service credential, never a caller-supplied token,
    to call Authentik's invitation-stage API. Requires the SystemAdmin role or above (skipped only
    under the local-test bypass).

    Args:
        email: The invitee's email address, at least 3 characters and shaped like `name@domain.tld`.
            Only this plausibility check is made here; Authentik is the real validation authority at
            redemption time.

    Returns:
        `{"itoken": <invitation key>, "invite_url": <redemption URL>}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller is not authenticated or lacks the required access role.
        - Authentik is unreachable or returns a non-2xx response.
        - The audit trail is unavailable: a `user.invite` row is written before Authentik is called,
          and if it cannot be written nothing is sent.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Creates an invitation in Authentik. Writes a `user.invite` audit row before the call, and a
        follow-up row if the call fails. On success it also logs an `invitations` entry naming the
        invitee's email.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        audit = _audit_context(config, actor)
        if isinstance(audit, str):
            return audit
        try:
            result = invite_user_audited(
                config, email, audit=audit, send_invitation=create_invitation
            )
        except (AuthentikInvitationError, AuditTrailUnavailableError) as exc:
            return f"error: {exc}"
        _emit_invite_created_audit_entry(actor=actor, principal=principal, email=email)
        return {"itoken": result.itoken, "invite_url": result.invite_url}

    return _run_mcp_action("invite_user", principal, _body)


@_gated_tool("get-catalog-listing", _PLAIN_READ_GATE)
def get_catalog_listing() -> dict[str, object] | str:
    """List every curated instrument, external and internal, in the curated-content source.

    Resolves the effective curated-content source (a persisted override when one exists, else the
    configured default), then fetches and parses its `catalog.json`. It fails closed: when the
    override cannot be read this tool returns an error rather than listing the default source. Takes
    no parameters.

    Returns:
        A dict with an `instruments` list, one entry per curated instrument, unfiltered, each with
        `instrument_id`, `title`, `source_type` and `jurisdiction` (`None` for an internal-source
        entry). Use an `instrument_id` with `restore_instrument`.

    Errors (returned as a string beginning `error: `, never raised):
        - The override cannot be read (the runtime configuration store is unavailable).
        - The curated-content source is unreachable or returns a missing or malformed
          `catalog.json`.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        One network fetch of `catalog.json`; nothing is written. The call is logged.
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


@_gated_tool("restore_instrument", _COMPLIANCE_OFFICER_GATE)
def restore_instrument(
    instrument_id: Annotated[
        str,
        Field(pattern=_RESTORE_INSTRUMENT_ID_PATTERN),
        AfterValidator(_reject_path_traversal_segment),
    ],
) -> dict[str, object] | str:
    """Restore one curated instrument from the curated-content source into the compliance graph.

    Resolves `instrument_id` against the curated catalog without regard to case (`cra-1.0` resolves
    to
    the catalog's own `CRA-1.0`), fetches that instrument's manifest, baseline and native artifact
    from
    the effective curated-content source (a persisted override when one exists, else the configured
    default), verifies the artifact, and restores it into the graph. Requires the ComplianceOfficer
    role
    (skipped only under the local-test bypass). The restoring caller becomes the owner of any draft
    Policy the restore imports.

    Args:
        instrument_id: Catalog id of the instrument, for example `CRA-1.0`; must match the allowed
            id pattern and must not contain `..`. A value that violates either rule is rejected
            before
            the tool runs.

    Returns:
        A dict with `instrument_id`, a `stages` list holding one entry per completed restore
        stage, each with its own `stage` and `status`, and `near_miss_reviews_created`: the
        number of Capability near-miss reviews the restore recorded (resolve them with
        `near_misses_list` and `near_misses_resolve`).

    Errors (returned as a string beginning `error: `, never raised):
        - The caller is not authenticated or lacks the required access role.
        - The audit trail cannot record the opening row; nothing is restored.
        - The persisted source override cannot be read, or the curated-content source is unreachable
          or
          returns a missing or malformed artifact.
        - `instrument_id` matches more than one catalog entry when case is ignored (the message
          names
          every colliding id), or matches no entry (the message names the closest ids); nothing is
          fetched in either case.
        - The artifact fails checksum or `schema_version` verification, or a restore stage fails
          (including a missing `PS_COMPANYMERGE_SIMILARITY_THRESHOLD` setting).
        - The policy graph database cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Fetches the catalog and the artifact over the network. Writes an `instrument.restore` audit
        event before the restore starts and a terminal one afterwards. Writes the restored
        instrument,
        and any imported draft Policy owned by the caller, to the policy graph.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _resolve_authz_actor(config)
    # The restoring caller owns any imported draft Policy. Same resolution as the
    # policy-lifecycle tools (verified `(sub, iss)`, or the local-test bypass identity).
    owner = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        request_body = CatalogRestorationRequest(instrument_id=instrument_id)
        dependencies: CatalogRestoreDependencies = _sanitize_restore_graph_opens(
            build_default_restore_from_catalog_dependencies()
        )
        audit = _audit_context(config, actor)
        if isinstance(audit, str):
            return audit
        try:
            outcome = run_restoration_from_catalog_source(
                request_body,
                config=config,
                actor=principal or "unknown",
                dependencies=dependencies,
                audit=audit,
                owner=owner,
            )
        except (
            AuditTrailUnavailableError,
            CatalogSourceOverrideUnavailableError,
            CuratedSourceUnavailableError,
            RestoreInstrumentIdAmbiguousError,
            RestoreInstrumentIdNotFoundError,
            GraphWriteUnavailableError,
            RestoreArtifactRejectedError,
            RestoreStageFailedError,
        ) as exc:
            return f"error: {exc}"
        except McpGraphUnavailableError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return outcome.model_dump()

    return _run_mcp_action("restore_instrument", principal, _body)


# `celex` is a plain node PROPERTY on `RegulatoryInstrument`, never its node id
# (`{SHORT}-{VERSION}`).
# Fixed, code-owned, never string-interpolated -- `celex_ids` always flows in
# via `params` (L2 Query Safety).
_INGESTION_STATUS_QUERY = (
    "UNWIND $celex_ids AS c "
    "MATCH (n:RegulatoryInstrument {celex: c}) "
    "RETURN DISTINCT n.celex AS celex"
)


@_gated_tool("check_instrument_ingestion_status", _PLAIN_READ_GATE)
def check_instrument_ingestion_status(
    celex_ids: Annotated[
        list[Annotated[str, Field(min_length=10, max_length=10, pattern=_CELEX_PATTERN)]],
        Field(min_length=1, max_length=25),
    ],
) -> dict[str, object] | str:
    """Report whether each of the given EU instruments is already ingested into the policy graph.

    Batch every candidate CELEX id into one call to learn which instruments the graph already
    tracks,
    instead of querying them one at a time. An instrument counts as ingested when the graph holds a
    `RegulatoryInstrument` whose `celex` property equals the id (a plain property, distinct from the
    node's own `{SHORT}-{VERSION}` id). It needs no elevated access role; it is a plain graph read.

    Args:
        celex_ids: 1 to 25 CELEX ids, each exactly 10 characters matching the CELEX pattern used by
            `ingest_regulation`. A violation is rejected before the tool runs. Duplicates collapse
            to a
            single entry in the result.

    Returns:
        `{"statuses": {<celex>: "ingested" | "not_yet_ingested" | "unknown", ...}}`, one entry per
        distinct requested id. A graph problem never fails the call: when the graph cannot be
        reached,
        has no seeded content at all, or rejects the lookup, every requested id is reported as
        `"unknown"` rather than `"not_yet_ingested"`, because a graph in that state more likely
        signals
        a provisioning or connectivity problem than a genuinely empty one.

    Errors (returned as a string beginning `error: `, never raised):
        - Any unexpected failure outside the graph lookup (a fixed generic message).

    Side Effects:
        One read-only graph query; nothing is written. The call is logged.
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


def _record_cypher_call(
    config: ServiceConfig, actor: tuple[str, str] | None, outcome: CypherCallOutcome
) -> None:
    """Write the one `query.cypher` audit row of a `cypher` tool call; BEST-EFFORT.

    The row records the verified caller (or the local-test-bypass sentinel), the outcome class and
    the enumerated facts of `outcome`; never the query text or an error text. When no audit actor
    can be resolved (no verified caller and the bypass is off) no row is written and one failed log
    entry names the error class. A failed audit write is logged by `record_follow_up_row` and never
    changes the tool's result.

    Args:
        config: Service configuration; selects the audit database and the bypass state.
        actor: Verified `(sub, iss)` of the caller, or `None` when none is bound.
        outcome: How the call ended.

    Side Effects:
        Inserts one `audit_events` row (`applied` when `reason_code` is `None`, `rejected` for a
        write clause, `failed` otherwise).
    """
    audit = _audit_context(config, actor)
    if isinstance(audit, str):
        emit_log_entry(
            component=_COMPONENT,
            action="cypher_audit_skipped",
            outcome="failed",
            extra={"reason": AuditActorUnresolvedError.__name__},
        )
        return
    reason_code = outcome.reason_code
    details: dict[str, object] = (
        {"row_count": outcome.row_count, "truncated": outcome.truncated}
        if reason_code is None
        else {"reason_code": reason_code}
    )
    audit_outcome: Literal["applied", "rejected", "failed"] = "failed"
    if reason_code is None:
        audit_outcome = "applied"
    elif reason_code == "write_clause":
        audit_outcome = "rejected"
    record_follow_up_row(
        audit,
        AuditTarget(QUERY_CYPHER_ACTION, QUERY_RESOURCE_TYPE, QUERY_CYPHER_RESOURCE_ID),
        component=_COMPONENT,
        outcome=audit_outcome,
        details=details,
    )


@_gated_tool("cypher", _PLAIN_READ_GATE)
def cypher(query: str) -> dict[str, object] | str:
    """Run a read-only, MATCH/RETURN-shaped Cypher query against the policy_system graph.

    Call `domain_concepts` first and ground every query in the labels, properties and edges it
    lists.
    The query runs with the configured timeout and row cap.
    Policy, Standard and Control content is visible in every status, `draft` included, so filter
    on `status` when only approved content is wanted.

    Args:
        query: Cypher text. It must not contain a write clause (CREATE, MERGE, DELETE, SET, REMOVE,
            DROP, FOREACH); such a query is rejected before execution.

    Returns:
        A dict with `columns`, `rows`, `row_count` and `truncated` (`true` when more rows matched
        than
        the row cap returned). A query that legitimately matches nothing returns the normal empty
        shape.

    Errors (returned as a string beginning `error: `, never raised):
        - The query contains a write clause.
        - The graph has no seeded content at all yet.
        - The graph database rejects or fails to execute the query.
        - The graph database cannot be reached.

    Side Effects:
        One read-only graph query; nothing is written to the graph. The call is logged, and one
        `query.cypher` audit row records the outcome (applied, rejected or failed) with the row
        count, the truncation flag and an enumerated reason code, never the query text. The audit
        write is best-effort: a failure there is logged and does not change the result. No row is
        written when the service configuration cannot be loaded.
    """
    try:
        config = load_config()
    except ServiceConfigurationError:
        emit_log_entry(component=_COMPONENT, action=_ACTION, outcome="unavailable")
        return _GRAPH_UNAVAILABLE_MESSAGE
    actor = _resolve_authz_actor(config)
    try:
        graph = _resolve_graph(config)
    except McpGraphUnavailableError:
        emit_log_entry(component=_COMPONENT, action=_ACTION, outcome="unavailable")
        _record_cypher_call(
            config,
            actor,
            CypherCallOutcome(_GRAPH_UNAVAILABLE_MESSAGE, None, None, "graph_unavailable"),
        )
        return _GRAPH_UNAVAILABLE_MESSAGE
    outcome = run_cypher_for_tool(
        query,
        graph=graph,
        principal=_resolve_principal(config),
        timeout_ms=config.query_timeout_ms,
        row_cap=config.query_row_cap,
    )
    _record_cypher_call(config, actor, outcome)
    return outcome.response


@_gated_tool("domain_concepts", _PLAIN_READ_GATE)
def domain_concepts() -> str:
    """Return the PS compliance-graph schema: node labels, properties and edges.

    Call this first and ground every Cypher query in it. Takes no parameters.

    Returns:
        Text listing each node label with its properties (type, enum values, required flag) and each
        edge with direction, cardinality and edge-property names. Prose explanations and worked
        examples
        are not included; read the `psdomain://concepts` resource for those.

    Errors:
        None; the text is built from the schema compiled into the service.

    Side Effects:
        None.
    """
    return render_slim_schema(DOMAIN_SCHEMA)


def _load_domain_concepts() -> str:
    """Read the packaged ps-domain-concepts.md text.

    Takes no parameters, so no client-supplied input reaches the file read.

    Returns:
        The full UTF-8 text of the packaged file.

    Raises:
        McpResourceUnavailableError: The file cannot be read; the message is a fixed, path-free
        detail.

    Side Effects:
        Reads one file from the installed package.
    """
    try:
        return _domain_concepts_path().read_text(encoding="utf-8")
    except OSError as exc:
        raise McpResourceUnavailableError(_DOMAIN_CONCEPTS_UNAVAILABLE_DETAIL) from exc


@server.resource(
    _DOMAIN_CONCEPTS_URI,
    name="ps-domain-concepts",
    title="PS domain concepts",
    description="The canonical PS compliance-graph vocabulary and schema, served verbatim.",
    mime_type="text/markdown",
)
def read_domain_concepts() -> str:
    """Return the full ps-domain-concepts.md text.

    This is the canonical PS compliance-graph vocabulary and schema, served verbatim.

    Takes no parameters.

    Returns:
        The file text verbatim. If the file is unavailable, a string beginning `error: ` followed by
        a
        fixed detail that carries no path and no cause.

    Errors:
        None raised; an unreadable file is reported through the returned string.

    Side Effects:
        Reads one file from the installed package.
    """
    try:
        return _load_domain_concepts()
    except McpResourceUnavailableError:
        return f"error: {_DOMAIN_CONCEPTS_UNAVAILABLE_DETAIL}"


def _assignment_to_dict(row: AccessRoleAssignmentRow) -> dict[str, object]:
    """Shape one access-role assignment row for the `list-access-roles` response.

    Args:
        row: The assignment row read from the authorization store.

    Returns:
        A dict with `principal_subject`, `principal_issuer`, `access_role` (the role's string
        value),
        `granted_at` (ISO 8601 text), `granted_by_subject` and `granted_by_issuer`.

    Side Effects:
        None.
    """
    return {
        "principal_subject": row.principal_subject,
        "principal_issuer": row.principal_issuer,
        "access_role": row.access_role.value,
        "granted_at": row.granted_at.isoformat(),
        "granted_by_subject": row.granted_by_subject,
        "granted_by_issuer": row.granted_by_issuer,
    }


@_gated_tool("list-access-roles", _ACCESS_ROLE_MANAGEMENT_GATE)
def list_access_roles() -> dict[str, object] | str:
    """List every principal's current access-role assignments.

    Takes no parameters. The very first caller ever, of any identity, is bootstrapped to
    `AuthenticatedUser` plus `SystemOwner` before the access check runs, so that call always
    succeeds.
    Every later principal holds `AuthenticatedUser` alone by default, and this tool then requires
    `SystemAdmin` or `SystemOwner`. Requires a real authenticated session; the local-test bypass
    does
    not provide one.

    Returns:
        `{"assignments": [{"principal_subject", "principal_issuer", "access_role", "granted_at",
        "granted_by_subject", "granted_by_issuer"}, ...], "system_owner_floor_warning": bool}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - The caller lacks the required role.
        - The authorization store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the authorization store. The first-ever call also writes the bootstrap assignments and
        their audit events. The call is logged.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _gated_actor(config)

    def _body() -> dict[str, object] | str:
        store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
        try:
            result = list_assignments(actor, store=store)
        except AuthorizationStoreUnavailableError as exc:
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


@_gated_tool("grant-access-role", _ACCESS_ROLE_CHANGE_GATE)
def grant_access_role(
    principal_subject: Annotated[str, Field(min_length=1)],
    access_role: Literal["SystemOwner", "SystemAdmin", "PolicyManager", "ComplianceOfficer"],
) -> dict[str, object] | str:
    """Grant an access role to a principal.

    Granting `SystemAdmin` or `SystemOwner` requires the caller to hold `SystemOwner`. Granting
    `PolicyManager` or `ComplianceOfficer` requires `SystemOwner` or `SystemAdmin`. A caller may
    never
    grant a role to themselves. Requires a real authenticated session; the local-test bypass does
    not
    provide one.

    Args:
        principal_subject: Subject (`sub`) of the principal who receives the role; at least 1
            character.
        access_role: One of `SystemOwner`, `SystemAdmin`, `PolicyManager` or `ComplianceOfficer`;
            any
            other value is rejected before the tool runs.

    Returns:
        `{"principal_subject", "access_role", "granted_by_subject", "system_owner_floor_warning"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `access_role` is not grantable.
        - The caller lacks the role the grant requires.
        - `principal_subject` is the caller.
        - The authorization store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Persists the assignment in the authorization store and writes an audit event.
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


@_gated_tool("revoke-access-role", _ACCESS_ROLE_CHANGE_GATE)
def revoke_access_role(
    principal_subject: Annotated[str, Field(min_length=1)],
    access_role: Literal["SystemOwner", "SystemAdmin", "PolicyManager", "ComplianceOfficer"],
) -> dict[str, object] | str:
    """Revoke an access role from a principal.

    Revoking `SystemOwner` requires the caller to hold `SystemOwner` or `SystemAdmin`. Revoking
    `SystemAdmin`, `PolicyManager` or `ComplianceOfficer` requires the same role that granting it
    requires. A caller may never revoke a role from themselves; that check runs before the floor
    check,
    which rejects revoking the last active `SystemOwner` whoever the caller is. Requires a real
    authenticated session; the local-test bypass does not provide one.

    Args:
        principal_subject: Subject (`sub`) of the principal who loses the role; at least 1
            character.
        access_role: One of `SystemOwner`, `SystemAdmin`, `PolicyManager` or `ComplianceOfficer`;
            any
            other value is rejected before the tool runs.

    Returns:
        `{"principal_subject", "access_role", "revoked_by_subject", "system_owner_floor_warning"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `access_role` is not one of the four managed roles.
        - The caller lacks the role the revocation requires.
        - `principal_subject` is the caller.
        - The revocation would leave zero active `SystemOwner` principals.
        - The authorization store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Updates the assignment in the authorization store and writes an audit event.
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
    """Shape one audit event row for the `list-audit-events` response.

    Args:
        row: The audit event row read from the audit store.

    Returns:
        A dict with `id`, `occurred_at` (ISO 8601 text), `actor_subject`, `actor_issuer`, `action`,
        `resource_type`, `resource_id`, `outcome` and `details`.

    Side Effects:
        None.
    """
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
    InvalidAuditQueryFilterError,
    AuthorizationStoreUnavailableError,
)


@_gated_tool("list-audit-events", _ACCESS_ROLE_MANAGEMENT_GATE)
def list_audit_events(  # noqa: PLR0913, PLR0917 -- every parameter is an independent, optional query filter; collapsing them into one payload object would just move the same count behind a wrapper
    actor_subject: Annotated[str, Field(min_length=1)] | None = None,
    actor_issuer: Annotated[str, Field(min_length=1)] | None = None,
    resource_type: Annotated[str, Field(min_length=1)] | None = None,
    resource_id: Annotated[str, Field(min_length=1)] | None = None,
    action: Annotated[str, Field(min_length=1)] | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    cursor: Annotated[str, Field(min_length=1)] | None = None,
    page_size: Annotated[int, Field(gt=0, le=100)] = 25,
    details: dict[str, str] | None = None,
) -> dict[str, object] | str:
    """Read the shared audit trail, filtered and paginated, newest first.

    Requires `SystemOwner` or `SystemAdmin`, because the trail is at least as sensitive as the role
    roster it partly documents, and a real authenticated session; the local-test bypass does not
    provide one. Every parameter is optional and filters combine.

    The trail includes the denials of role-gated operations (`access.gate`, resource type
    `operation`, the operation name as the resource id), passkey enrolments (`passkey.enroll`,
    resource type `pending_approval`) and `cypher` tool use (`query.cypher`, resource type
    `query`), next to the access-role, policy, ingestion, restore and other business rows. It does
    not hold authentication outcomes or rejected passkey steps.

    Args:
        actor_subject: Narrow to events by this actor subject; at least 1 character.
        actor_issuer: Narrow to events by this actor issuer; at least 1 character.
        resource_type: Narrow to this kind of acted-on resource; at least 1 character.
        resource_id: Narrow to this acted-on resource id; at least 1 character.
        action: Narrow to this namespaced action string, for example `access_role.grant`,
            `access.gate`, `passkey.enroll` or `query.cypher`; at least 1 character.
        occurred_from: Only events at or after this ISO 8601 time.
        occurred_to: Only events at or before this ISO 8601 time.
        cursor: The `next_cursor` of a previous call, to fetch the following page; at least 1
            character.
        page_size: Events per page, from 1 to 100; default 25.
        details: Exact-match filter on the business subject of a row. Allowed keys are `celex` (an
            ingestion run's submit and complete rows), `regulatory_instrument_id` (an ingestion's
            complete row) and `instrument_id` (restore rows), for example `{"celex": "32024R2847"}`.
            Any other key, or an empty value, is rejected.

    Returns:
        `{"events": [{"id", "occurred_at", "actor_subject", "actor_issuer", "action",
        "resource_type",
        "resource_id", "outcome", "details"}, ...], "next_cursor": str | None}`; `next_cursor` is
        `None` once no further events remain.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - The caller lacks the required role.
        - A filter, `page_size` or `cursor` is invalid, including a disallowed `details` key.
        - The audit or authorization store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the audit trail; nothing is written. The call is logged.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _gated_actor(config)

    def _body() -> dict[str, object] | str:
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
            details=details,
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


_VERIFY_AUDIT_LOG_ERRORS = (AuthorizationStoreUnavailableError,)


def _chain_break_to_dict(verification: AuditChainVerification) -> dict[str, object] | None:
    """The `first_break` object of `verify-audit-log`, or `None` for an intact chain.

    A row break names the row (`chain_seq`, `id`, `occurred_at`). A `head_mismatch` has no broken
    row, so it carries the head sequence instead.
    """
    first_break = verification.first_break
    if first_break is None:
        return None
    if first_break.kind == "head_mismatch":
        return {"kind": first_break.kind, "head_chain_seq": verification.head_chain_seq}
    return {
        "kind": first_break.kind,
        "chain_seq": first_break.chain_seq,
        "id": first_break.event_id,
        "occurred_at": (
            None if first_break.occurred_at is None else first_break.occurred_at.isoformat()
        ),
    }


@_gated_tool("verify-audit-log", _ACCESS_ROLE_MANAGEMENT_GATE)
def verify_audit_log() -> dict[str, object] | str:
    """Check that the shared audit trail has not been altered, deleted from or truncated.

    Requires `SystemOwner` or `SystemAdmin` and a real authenticated session; the local-test
    bypass does not provide one and is refused before any verification runs. Takes no
    arguments. The database recomputes every row's hash in order and compares the last row with
    the recorded chain head, in one consistent snapshot, so the check is safe to run while the
    service is writing.

    Returns:
        `{"intact": bool, "rows_checked": int, "head_chain_seq": int, "first_break": null | {...}}`.
        `intact` is `true` and `first_break` is `null` when the chain runs unbroken from its
        first row to the head. Otherwise `first_break` is the lowest-numbered failure: a row
        break is `{"kind", "chain_seq", "id", "occurred_at"}` with `kind` one of `sequence_gap`
        (a row is missing), `prev_hash_mismatch` or `row_hash_mismatch` (the row was altered);
        `{"kind": "head_mismatch", "head_chain_seq"}` means every remaining row verifies but
        the newest rows are gone (the tail was truncated).

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - The caller lacks the required role.
        - The audit or authorization store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the audit trail; nothing is written. The call is logged. The check is
        tamper-evident, not tamper-proof: a database superuser who rewrites the whole chain and
        its head consistently is not detected.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)
    actor = _gated_actor(config)

    def _body() -> dict[str, object] | str:
        access_role_store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
        audit_store = PsycopgAuditStore(config)
        try:
            verification = run_verify_audit_log(
                actor, access_role_store=access_role_store, audit_store=audit_store
            )
        except _VERIFY_AUDIT_LOG_ERRORS as exc:
            return f"error: {exc}"
        return {
            "intact": verification.intact,
            "rows_checked": verification.rows_checked,
            "head_chain_seq": verification.head_chain_seq,
            "first_break": _chain_break_to_dict(verification),
        }

    return _run_mcp_action("verify_audit_log", principal, _body)


_POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE = (
    "error: this action requires a real authenticated caller (the local-test bypass counts as one)"
)

# `approve-policy` / `reject-policy` keep their check order (the Policy is read first, so an unknown
# id is reported before an access denial), so the role gate is a body gate: the policy-lifecycle
# service calls the enforcer the tool hands it, at the existing position. Under the bypass the role
# is checked for the lifecycle fallback identity and the audit row names the bypass sentinel.
_POLICY_TRANSITION_GATE = ToolGate(
    kind=GateKind.BODY,
    role=AccessRole.POLICY_MANAGER,
    bypass=BypassPolicy.LIFECYCLE_IDENTITY,
    unauthenticated_message=_POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE,
)


class _MalformedPatchFieldsError(Exception):
    """A `fields` argument to one of the draft-content tools is shaped wrong.

    Raised only by `_parse_patch_fields`; each draft-content tool catches it at its own call site
    and
    returns it as an `error: ` string.
    """


def _parse_patch_fields(
    fields: dict[str, object] | None,
    *,
    allowed: frozenset[str],
    enum_checks: Mapping[str, tuple[str, ...]] = MappingProxyType({}),
) -> dict[str, object]:
    """Validate `fields` against a tool's allow-list of patchable field names.

    `None` (the parameter omitted) is a no-op update and yields `{}`. Every key must be in `allowed`
    and every value must be a string or `None`. `None` clears the field in an update tool and leaves
    it
    unset in a create tool; it is distinct from an absent key, which leaves any existing value
    alone.

    Args:
        fields: The raw `fields` argument as the MCP caller supplied it, or `None`.
        allowed: The fixed set of field names the calling tool accepts.
        enum_checks: Field name to the tuple of values that field is restricted to; checked only
            when
            the field is present and not `None`.

    Returns:
        `fields` itself once every key and value is validated, or `{}` when `fields` is `None`.

    Raises:
        _MalformedPatchFieldsError: A key is not in `allowed`, a value is neither a string nor
        `None`,
            or a restricted field holds a value outside its tuple.

    Side Effects:
        None.
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
    GraphWriteUnavailableError,
    PolicyCapabilityAlreadyGovernedError,
    PolicyCapabilityNotFoundError,
    PolicyNotFoundError,
    PolicySupersedePriorNotApprovedError,
    PolicyTitleAlreadyExistsError,
    PolicyLifecycleGraphUnavailableError,
)

_CREATE_POLICY_DRAFT_CONTROL_TYPES = ("automated", "manual")


class _MalformedPolicyDraftStandardsError(Exception):
    """The `standards` argument of `create-policy-draft` is shaped wrong.

    Raised only by `_parse_policy_draft_standards` and the helpers it calls; `create_policy_draft`
    catches it and returns it as an `error: ` string. The message names the offending
    `standards[i]` or `standards[i].controls[j]` position so a caller can locate the bad entry.
    """


def _require_standards_str_field(body: dict[str, object], field: str, *, where: str) -> str:
    """Return a required non-empty string field of one `standards` entry.

    Args:
        body: The `standards[i]` or `standards[i].controls[j]` object being parsed.
        field: Name of the key to read from `body`.
        where: Position label of `body` (for example `standards[0]`), used in the error message.

    Returns:
        The non-empty string stored under `field`.

    Raises:
        _MalformedPolicyDraftStandardsError: `field` is absent, not a string, or empty.
    """
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise _MalformedPolicyDraftStandardsError(f"{where}.{field} must be a non-empty string")
    return value


def _parse_control_draft_input(raw: object, *, where: str) -> ControlDraftInput:
    """Parse one `standards[i].controls[j]` entry into a `ControlDraftInput`.

    Args:
        raw: The entry as supplied by the caller.
        where: Position label of the entry, used in error messages.

    Returns:
        A `ControlDraftInput` with the entry's `title` and its `control_type` (default `"manual"`).

    Raises:
        _MalformedPolicyDraftStandardsError: `raw` is not an object, `title` is missing or empty, or
            `control_type` is not `"automated"` or `"manual"`.
    """
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
    """Parse one `standards[i]` entry, with its optional `controls`, into a `StandardDraftInput`.

    Args:
        raw: The entry as supplied by the caller.
        where: Position label of the entry, used in error messages.

    Returns:
        A `StandardDraftInput` with the entry's `title` and a tuple of parsed controls (empty when
        `controls` is omitted).

    Raises:
        _MalformedPolicyDraftStandardsError: `raw` is not an object, `title` is missing or empty,
            `controls` is not a list, or a control entry is malformed.
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
    """Parse the optional `standards` argument of `create-policy-draft`, top to bottom.

    Args:
        standards: The list of Standard objects as supplied by the caller, or `None` when omitted.

    Returns:
        One `StandardDraftInput` per entry, in order; an empty tuple when `standards` is `None`.

    Raises:
        _MalformedPolicyDraftStandardsError: An entry, or a nested control, is malformed.
    """
    if standards is None:
        return ()
    return tuple(
        _parse_standard_draft_input(item, where=f"standards[{index}]")
        for index, item in enumerate(standards)
    )


@_gated_tool("create-policy-draft", _POLICY_AUTHOR_GATE)
def create_policy_draft(
    title: Annotated[str, Field(min_length=1)],
    standards: list[dict[str, object]] | None = None,
    supersedes_policy_id: Annotated[str, Field(min_length=1)] | None = None,
    capability_ids: list[Annotated[str, Field(min_length=1)]] | None = None,
) -> dict[str, object] | str:
    """Create a new draft Policy owned by the caller, or an amendment of an approved Policy.

    Requires a real authenticated session (the local-test bypass counts as one). The Policy, and
    every
    Standard and Control created with it, starts in `draft` status.

    Args:
        title: Policy title; at least 1 character.
        standards: Optional list of objects `{"title": <str>, "controls": [...]}` (`controls`
            optional,
            default `[]`); each control is `{"title": <str>, "control_type": "automated" |
            "manual"}`
            (`control_type` optional, default `"manual"`). Every `title` must be a non-empty string.
            Omit it or pass `null` for a title-only draft. Ignored when `supersedes_policy_id` is
            set.
        supersedes_policy_id: Optional id of an `approved` Policy to amend. When omitted this
            creates an
            ordinary version-1 Policy whose id is derived from `title` alone, so repeating a title
            is an
            error. When set, it creates a successor draft whose id derives from both `title` and
            this
            id, with `version` one higher (still a string), a `SUPERSEDED_BY` edge from the prior
            Policy,
            and Standard and Control children forked as new, independently editable copies of the
            prior
            Policy's current children; the prior nodes are not changed. At least 1 character.
        capability_ids: Optional ids of existing Capabilities the new Policy will govern; each at
            least
            1 character. The governing edges are written together with the Policy, so it governs
            them
            from creation, while still a draft. Duplicates are dropped, order kept. Every id must
            name
            an existing Capability that no Policy governs yet. Ignored when `supersedes_policy_id`
            is
            set: a fork's Capabilities stay with the prior Policy and move to the successor when it
                is
            approved.

    Returns:
        `{"policy_id", "title", "status", "version", "owner_subject", "standard_ids", "control_ids",
        "superseded_policy_id", "governed_capability_ids"}`. `standard_ids` and `control_ids` list
        every
        created or forked child (empty for a title-only draft) and can be used in later
        draft-editing
        calls; `superseded_policy_id` is `null` unless this was an amendment;
        `governed_capability_ids`
        is empty when none are governed or on an amendment.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `standards` or a nested `controls` entry is malformed (not objects, a missing or empty
          `title`, or an invalid `control_type`).
        - `supersedes_policy_id` names no Policy, or a Policy that is not `approved`.
        - A `capability_ids` entry names no Capability or one that is already governed; nothing is
          written.
        - The computed Policy id already exists.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the Policy and its children to the policy graph in one guarded step and records an
        audit
        event.
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
            # Fail closed before any read or write: no write path, no lifecycle write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_create_policy_draft(
                actor=actor,
                title=title,
                standards=parsed_standards,
                supersedes_policy_id=supersedes_policy_id,
                capability_ids=tuple(capability_ids or ()),
                graph=graph,
                audit_store=audit_store,
                submitter=submitter,
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
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("update-policy-draft", _POLICY_DRAFT_ACCESS_GATE)
def update_policy_draft(
    policy_id: Annotated[str, Field(min_length=1)],
    fields: dict[str, object] | None = None,
) -> dict[str, object] | str:
    """Change a subset of a draft Policy's content fields, leaving the rest as they are.

    Usable only while the Policy is in `draft` status, and only by its owner or a caller holding
    `SystemOwner` or `SystemAdmin`. Requires a real authenticated session (the local-test bypass
    counts as one).

    Args:
        policy_id: Id of the Policy to change; at least 1 character.
        fields: Partial-update map; only the supplied keys change. Each value is a string or JSON
            `null`, which clears the field. Allowed keys: `description`, `scope_in`, `scope_out`,
            `normative_commitments`, `review_cadence`, `exception_pathway`, `measurable_outcomes`,
            `capability_grouping_rationale`. `title`, `status`, `owner_subject`, `owner_issuer` and
            `version` can never be patched. Omit it or pass `null` for a no-op update.

    Returns:
        `{"policy_id", "updated_fields"}`, where `updated_fields` is every key supplied in `fields`,
        sorted.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `fields` names an unknown key or a value that is neither a string nor `null`.
        - No Policy has `policy_id`.
        - The caller is neither the owner nor a `SystemOwner` or `SystemAdmin`.
        - The Policy is not in `draft` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the changed fields to the policy graph; no audit event is written. May also backfill
        a missing `version` and `status` on the Policy tree.
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
            # Fail closed before any read or write: no write path, no draft edit.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_update_policy_draft(
                actor=actor,
                policy_id=policy_id,
                fields=parsed_fields,
                graph=graph,
                access_role_store=access_role_store,
                submitter=submitter,
            )
        except _UPDATE_POLICY_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "updated_fields": list(result.updated_fields),
        }

    return _run_mcp_action("update_policy_draft", principal, _body, entity_id=policy_id)


_ADD_STANDARD_TO_DRAFT_ERRORS = (
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("add-standard-to-draft", _POLICY_DRAFT_ACCESS_GATE)
def add_standard_to_draft(
    policy_id: Annotated[str, Field(min_length=1)],
    title: Annotated[str, Field(min_length=1)],
    fields: dict[str, object] | None = None,
) -> dict[str, object] | str:
    """Add a new Standard under a draft Policy.

    Usable only while the Policy is in `draft` status, and only by its owner or a caller holding
    `SystemOwner` or `SystemAdmin`. Requires a real authenticated session (the local-test bypass
    counts
    as one). The Standard is linked to the Policy with `SUPPORTED_BY` and always starts in `draft`
    governance status, whatever its `implementation_status`.

    Args:
        policy_id: Id of the draft Policy; at least 1 character.
        title: Standard title; at least 1 character.
        fields: Optional content to set at creation. Each value is a string, or JSON `null`, which
            leaves the field unset. Allowed keys: `description`, `implementation_status`,
            `procedure`,
            `implementer_role`, `reviewer_role`, `applicability_boundary`, `verification_notes`,
            `change_rationale`. `implementation_status` must be one of `draft`, `implemented`,
            `reviewed`, `deprecated`, and defaults to `draft`. `title` and `status` cannot be set
            here.

    Returns:
        `{"standard_id", "policy_id", "title", "status"}`; `standard_id` can be used in later calls
        immediately.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `fields` names an unknown key, holds a value that is neither a string nor `null`, or holds
          an
          invalid `implementation_status`.
        - No Policy has `policy_id`.
        - The caller is neither the owner nor a `SystemOwner` or `SystemAdmin`.
        - The Policy is not in `draft` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the Standard and its link to the policy graph; no audit event is written.
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
            # Fail closed before any read or write: no write path, no draft write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_add_standard_to_draft(
                actor=actor,
                policy_id=policy_id,
                title=title,
                fields=parsed_fields,
                graph=graph,
                access_role_store=access_role_store,
                submitter=submitter,
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
    GraphWriteUnavailableError,
    PolicyStandardNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("update-standard-draft", _POLICY_DRAFT_ACCESS_GATE)
def update_standard_draft(
    standard_id: Annotated[str, Field(min_length=1)],
    fields: dict[str, object] | None = None,
) -> dict[str, object] | str:
    """Change a subset of a draft Standard's content fields, leaving the rest as they are.

    A Standard has no owner of its own: only the owner of its parent Policy, or a caller holding
    `SystemOwner` or `SystemAdmin`, may change it. Usable only while the Standard itself (not the
    parent Policy) is in `draft` status. Requires a real authenticated session (the local-test
    bypass
    counts as one).

    Args:
        standard_id: Id of the Standard to change; at least 1 character.
        fields: Partial-update map; only the supplied keys change. Each value is a string or JSON
            `null`, which clears the field. Allowed keys: `description`, `implementation_status`,
            `procedure`, `implementer_role`, `reviewer_role`, `applicability_boundary`,
            `verification_notes`, `change_rationale`. `implementation_status` must be one of
            `draft`,
            `implemented`, `reviewed`, `deprecated`. `title` and `status` can never be patched. Omit
            it
            or pass `null` for a no-op update.

    Returns:
        `{"standard_id", "policy_id", "title", "status"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `fields` names an unknown key, holds a value that is neither a string nor `null`, or holds
          an
          invalid `implementation_status`.
        - No Standard has `standard_id`.
        - The caller is neither the parent Policy's owner nor a `SystemOwner` or `SystemAdmin`.
        - The Standard is not in `draft` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the changed fields to the policy graph; no audit event is written.
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
            # Fail closed before any read or write: no write path, no draft write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_update_standard_draft(
                actor=actor,
                standard_id=standard_id,
                fields=parsed_fields,
                graph=graph,
                access_role_store=access_role_store,
                submitter=submitter,
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
    GraphWriteUnavailableError,
    PolicyStandardNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)

# `add-control-to-draft` excludes `"type"` from its `fields` allow-list: the top-level
# `control_type` parameter is the only way to set a Control's type at creation, and
# `update-control-draft` (which keeps the full `_CONTROL_PATCHABLE_FIELDS`) is the only
# post-creation path to change it.
_ADD_CONTROL_TO_DRAFT_PATCHABLE_FIELDS = _CONTROL_PATCHABLE_FIELDS - {"type"}


@_gated_tool("add-control-to-draft", _POLICY_DRAFT_ACCESS_GATE)
def add_control_to_draft(
    standard_id: Annotated[str, Field(min_length=1)],
    title: Annotated[str, Field(min_length=1)],
    control_type: Annotated[str, Field(min_length=1)] = "manual",
    fields: dict[str, object] | None = None,
) -> dict[str, object] | str:
    """Add a new Control under a draft Standard.

    Ownership comes from the Standard's parent Policy: only that Policy's owner, or a caller holding
    `SystemOwner` or `SystemAdmin`, may add a Control, and only while the Standard itself (not the
    parent Policy) is in `draft` status. Requires a real authenticated session (the local-test
    bypass
    counts as one). The Control is linked to the Standard with `IMPLEMENTED_BY` and always starts in
    `draft` governance status, whatever its `implementation_status`.

    Args:
        standard_id: Id of the draft Standard; at least 1 character.
        title: Control title; at least 1 character.
        control_type: `"automated"` or `"manual"`; default `"manual"`. This is the only way to set
            the
            type at creation.
        fields: Optional content to set at creation. Each value is a string, or JSON `null`, which
            leaves the field unset. Allowed keys: `description`, `implementation_status`,
            `execution_frequency`, `last_test_date`, `next_review_date`, `evidence_ref`,
            `pass_fail_criteria`, `execution_method`, `evidence_plan`, `executor_role`,
            `reviewer_role`,
            `risk_alignment_rationale`. `implementation_status` must be one of `planned`,
            `implemented`, `reviewed`, `deprecated`, and defaults to `planned`. `title`, `status`
            and
            `type` cannot be set here; `type` is rejected, use `control_type`.

    Returns:
        `{"control_id", "standard_id", "policy_id", "title", "status"}`; `control_id` can be used in
        later calls immediately.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `control_type` is not `"automated"` or `"manual"`.
        - `fields` names an unknown key (including `type`), holds a value that is neither a string
          nor
          `null`, or holds an invalid `implementation_status`.
        - No Standard has `standard_id`.
        - The caller is neither the parent Policy's owner nor a `SystemOwner` or `SystemAdmin`.
        - The Standard is not in `draft` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the Control and its link to the policy graph; no audit event is written.
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
            # Fail closed before any read or write: no write path, no draft write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_add_control_to_draft(
                actor=actor,
                standard_id=standard_id,
                title=title,
                control_type=control_type,
                fields=parsed_fields,
                graph=graph,
                access_role_store=access_role_store,
                submitter=submitter,
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
    GraphWriteUnavailableError,
    PolicyControlNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("update-control-draft", _POLICY_DRAFT_ACCESS_GATE)
def update_control_draft(
    control_id: Annotated[str, Field(min_length=1)],
    fields: dict[str, object] | None = None,
) -> dict[str, object] | str:
    """Change a subset of a draft Control's content fields, leaving the rest as they are.

    Ownership comes from the root Policy two levels up (Policy to Standard to Control): only that
    Policy's owner, or a caller holding `SystemOwner` or `SystemAdmin`, may change the Control.
    Usable
    only while the Control itself (not its Standard or Policy) is in `draft` status. Requires a real
    authenticated session (the local-test bypass counts as one).

    Args:
        control_id: Id of the Control to change; at least 1 character.
        fields: Partial-update map; only the supplied keys change. Each value is a string or JSON
            `null`, which clears the field. Allowed keys: `description`, `implementation_status`,
            `type`, `execution_frequency`, `last_test_date`, `next_review_date`, `evidence_ref`,
            `pass_fail_criteria`, `execution_method`, `evidence_plan`, `executor_role`,
            `reviewer_role`,
            `risk_alignment_rationale`. Unlike `add-control-to-draft`, `type` can be changed here,
            and
            this is the only way to change it after creation. `implementation_status` must be one of
            `planned`, `implemented`, `reviewed`, `deprecated`. `title` and `status` can never be
            patched. Omit it or pass `null` for a no-op update.

    Returns:
        `{"control_id", "standard_id", "policy_id", "title", "status"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `fields` names an unknown key, holds a value that is neither a string nor `null`, or holds
          an
          invalid `implementation_status`.
        - No Control has `control_id`.
        - The caller is neither the root Policy's owner nor a `SystemOwner` or `SystemAdmin`.
        - The Control is not in `draft` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the changed fields to the policy graph; no audit event is written.
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
            # Fail closed before any read or write: no write path, no draft write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_update_control_draft(
                actor=actor,
                control_id=control_id,
                fields=parsed_fields,
                graph=graph,
                access_role_store=access_role_store,
                submitter=submitter,
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


_GET_POLICY_ERRORS = (
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("get-policy", _POLICY_DRAFT_ACCESS_GATE)
def get_policy(policy_id: Annotated[str, Field(min_length=1)]) -> dict[str, object] | str:
    """Read one Policy together with its full Standard and Control tree.

    A Policy is visible to any authenticated caller in every status, `draft` included, so a draft
    can appear beside approved content; check `status` before treating a Policy as in force.
    Requires a real authenticated session (the local-test bypass counts as one).

    Args:
        policy_id: Id of the Policy to read; at least 1 character.

    Returns:
        `{"policy_id", "title", "status", "version", "owner_subject", "owner_issuer", "standards"}`,
        where `standards` is a list of `{"standard_id", "title", "status", "controls"}` and each
        `controls` is a list of `{"control_id", "title", "control_type", "status"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the policy graph. When an older Policy tree is missing data this read can backfill it,
        which writes to the graph; a tree that needs no backfill is not written.
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
        try:
            result = run_get_policy(
                policy_id=policy_id,
                graph=graph,
                # Resolved only if a backfill must be written (a read of a backfilled tree needs
                # no gateway).
                submitter_source=lambda: lifecycle_submitter(graph_write_access(config).gateway),
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
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyIncompleteForProposalError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("propose-policy", _POLICY_AUTHOR_GATE)
def propose_policy(policy_id: Annotated[str, Field(min_length=1)]) -> dict[str, object] | str:
    """Move a draft Policy and its whole tree to `proposed`.

    Only the Policy's owner may propose it, and only while it is in `draft` status with at least one
    Standard attached. Requires a real authenticated session (the local-test bypass counts as one).

    Args:
        policy_id: Id of the Policy to propose; at least 1 character.

    Returns:
        `{"policy_id", "status", "standard_ids", "control_ids"}`, listing every Standard and Control
        in
        the tree.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The caller does not own the Policy.
        - The Policy is not in `draft` status, or has no Standards.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Changes the status of the Policy and every Standard and Control in its tree in one cascading
        graph write and records an audit event.
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
            # Fail closed before any read or write: no write path, no lifecycle write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_propose_policy(
                actor=actor,
                policy_id=policy_id,
                graph=graph,
                audit_store=audit_store,
                submitter=submitter,
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
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    AccessDeniedError,
    PolicySelfApprovalBlockedError,
    PolicyInvalidStatusTransitionError,
    PolicyGovernanceConflictError,
    PolicyLifecycleGraphUnavailableError,
    AuthorizationStoreUnavailableError,
)


@_gated_tool("approve-policy", _POLICY_TRANSITION_GATE)
def approve_policy(policy_id: Annotated[str, Field(min_length=1)]) -> dict[str, object] | str:
    """Move a proposed Policy and its whole tree to `approved`.

    Requires the `PolicyManager` role, and the caller must not be the Policy's owner: self-approval
    is
    always refused, even for a `PolicyManager` who owns it. Usable only while the Policy is in
    `proposed` status. Requires a real authenticated session (the local-test bypass counts as one).
    When the Policy amends an approved prior Policy (created with `supersedes_policy_id`), that
    prior
    tree is deprecated in the same call, recorded as its own audit event. If the prior Policy
    governs
    Capabilities, their governing edges move to the approved Policy in the same all-or-nothing write
    as
    the status change.

    Args:
        policy_id: Id of the Policy to approve; at least 1 character.

    Returns:
        `{"policy_id", "status", "standard_ids", "control_ids", "auto_deprecated_policy_id",
        "governed_capability_ids"}`. `auto_deprecated_policy_id` is `None` unless a prior Policy was
        deprecated; `governed_capability_ids` lists the Capabilities whose governance moved (empty
        when
        none).

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The caller does not hold `PolicyManager`, or is the Policy's owner.
        - The Policy is not in `proposed` status.
        - The Capabilities governed by the superseded Policy changed during approval; nothing was
          changed, retry.
        - The policy graph or the authorization store cannot be reached, or the write path is
          unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Changes the status of the whole tree (and of the superseded tree, when there is one) and
        moves
        Capability governance in one graph write, and records audit events.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
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
            # Fail closed before any read or write: no write path, no lifecycle write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_approve_policy(
                actor=actor,
                policy_id=policy_id,
                graph=graph,
                audit_store=audit_store,
                enforce_policy_manager=lifecycle_role_enforcer(_gate_environment, "approve-policy"),
                submitter=submitter,
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
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    AccessDeniedError,
    PolicySelfApprovalBlockedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
    AuthorizationStoreUnavailableError,
)


@_gated_tool("reject-policy", _POLICY_TRANSITION_GATE)
def reject_policy(policy_id: Annotated[str, Field(min_length=1)]) -> dict[str, object] | str:
    """Move a proposed Policy and its whole tree back to `draft`.

    Requires the `PolicyManager` role, and the caller must not be the Policy's owner: self-rejection
    is
    always refused, even for a `PolicyManager` who owns it. Usable only while the Policy is in
    `proposed` status. Unlike approval, no prior Policy is deprecated. Requires a real authenticated
    session (the local-test bypass counts as one).

    Args:
        policy_id: Id of the Policy to reject; at least 1 character.

    Returns:
        `{"policy_id", "status", "standard_ids", "control_ids"}`, listing every Standard and Control
        in
        the tree.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The caller does not hold `PolicyManager`, or is the Policy's owner.
        - The Policy is not in `proposed` status.
        - The policy graph or the authorization store cannot be reached, or the write path is
          unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Changes the status of the Policy and every Standard and Control in its tree in one cascading
        graph write and records an audit event.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
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
            # Fail closed before any read or write: no write path, no lifecycle write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_reject_policy(
                actor=actor,
                policy_id=policy_id,
                graph=graph,
                audit_store=audit_store,
                enforce_policy_manager=lifecycle_role_enforcer(_gate_environment, "reject-policy"),
                submitter=submitter,
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
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("revert-policy-to-draft", _POLICY_AUTHOR_GATE)
def revert_policy_to_draft(
    policy_id: Annotated[str, Field(min_length=1)],
) -> dict[str, object] | str:
    """Move a proposed Policy and its whole tree back to `draft`, as its owner.

    Owner-only: unlike `reject-policy` there is no `PolicyManager` role check, and a `PolicyManager`
    who is not the owner is refused like any other non-owner. Usable only while the Policy is in
    `proposed` status. Requires a real authenticated session (the local-test bypass counts as one).

    Args:
        policy_id: Id of the Policy to revert; at least 1 character.

    Returns:
        `{"policy_id", "status", "standard_ids", "control_ids"}`, listing every Standard and Control
        in
        the tree.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The caller does not own the Policy.
        - The Policy is not in `proposed` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Changes the status of the Policy and every Standard and Control in its tree in one cascading
        graph write and records an audit event.
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
            # Fail closed before any read or write: no write path, no lifecycle write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_revert_policy_to_draft(
                actor=actor,
                policy_id=policy_id,
                graph=graph,
                audit_store=audit_store,
                submitter=submitter,
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


@_gated_tool("find-capability-merge-candidates", _CLEANUP_GATE)
def find_capability_merge_candidates_tool(
    min_similarity: Annotated[float | None, Field(gt=0.5, le=1.0)] = None,
) -> dict[str, object] | str:
    """List groups of active Capabilities that look like duplicates.

    This is a read-only graph cleanup aid.

    Nothing is changed and no approval is created. Requires an explicit `ComplianceOfficer` grant on
    a
    real authenticated session (no admin override; never available under the local-test bypass).
    Capabilities are linked when the cosine similarity of their already-cached embeddings reaches
    the
    threshold; no embedding is fetched or computed by this tool.

    Args:
        min_similarity: Similarity threshold, greater than 0.5 and at most 1.0. When omitted, the
            configured similarity threshold applies, else 0.90.

    Returns:
        `{"groups": [{"basis", "merge_case", "policies_distinct", "members": [{"id", "name",
        "obligation_count", "governing_policy"}, ...]}, ...]}`. `basis` is `"name"` when every
        member's
        name is equal once case and punctuation are ignored, else `"embedding"`. `governing_policy`
        is
        `{"id", "title", "status"}` or `null`; `obligation_count` is the number of Obligations
        requiring
        that Capability. `merge_case` is 1 (no member governed), 2 (exactly one governed) or 3 (two
        or
        more governed); `policies_distinct` is true when the governed members name different
        Policies.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session or lacks the `ComplianceOfficer` role.
        - The authorization store or the policy graph cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the policy graph and the authorization store; nothing is written. The call is logged.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
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


@_gated_tool("find-duplicate-obligations", _CLEANUP_GATE)
def find_duplicate_obligations_tool(role_id: str | None = None) -> dict[str, object] | str:
    """List groups of duplicate Obligations under one Role, as a read-only graph cleanup aid.

    Nothing is changed and no approval is created. Requires an explicit `ComplianceOfficer` grant on
    a
    real authenticated session (no admin override; never available under the local-test bypass).
    Only
    Obligations under the same Role are grouped, and only when their text is identical or
    near-identical
    once case and punctuation are ignored; the same wording under two Roles is never grouped.

    Args:
        role_id: Optional Role id; when given, the sweep covers only that Role.

    Returns:
        `{"groups": [{"role_id", "role_name", "basis", "members": [{"id", "text", "requirements":
        [{"requirement_id", "source_ref"}, ...]}, ...]}, ...]}`. `basis` is `"identical_text"` or
        `"near_text"`. Each `source_ref` comes from the `EXPRESSES` edge of a Requirement the
        Obligation
        satisfies.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session or lacks the `ComplianceOfficer` role.
        - The authorization store or the policy graph cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the policy graph and the authorization store; nothing is written. The call is logged.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
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


@_gated_tool("merge-capabilities", _CLEANUP_GATE)
def merge_capabilities_tool(
    survivor_id: Annotated[str, Field(min_length=1)],
    absorbed_id: Annotated[str, Field(min_length=1)],
    ctx: Context,
    *,
    acknowledge_governance_change: bool = False,
) -> dict[str, object] | str:
    """Preview merging one Capability into another and request a passkey approval for the merge.

    Requires an explicit `ComplianceOfficer` grant on a real authenticated session (no admin
    override;
    never available under the local-test bypass). This call never edits the graph: it returns a
    preview
    and creates a pending passkey approval bound to this exact pair and to the state previewed. The
    merge runs only when the officer opens `approval_url` in a browser and signs with a passkey;
    `check-cleanup-approval` reports the outcome.

    On execution the absorbed Capability's `REQUIRES`, `COVERS` and `MITIGATED_BY` edges move to the
    survivor with no duplicate edges, and the absorbed node is kept as a tombstone (`status`
    `merged`,
    `MERGED_INTO` the survivor). The change is one all-or-nothing graph write that is audited first
    (`capability.merge`).

    Merge case 1 (neither Capability has a governing Policy) needs nothing more. Case 2 (exactly one
    is
    governed) changes which Capability that Policy governs: the first call, without
    `acknowledge_governance_change`, returns the preview with `acknowledgment_required: true` and a
    `message`, and creates no approval; repeat the call with `acknowledge_governance_change=true`
    once
    the officer has explicitly accepted that change, and the acknowledgment becomes part of the
    signed
    approval and the audit row (`policy_case` 2). The preview's `governance` block names the Policy
    (`id`, `title`, `status`), `obligations_coverage_changed`, and the Policy's governed set before
    and
    after; an `approved` Policy keeps its content and version. Case 3 (both governed by the same
    Policy)
    needs no acknowledgment: the absorbed Capability's `GOVERNED_BY` edge is deleted and the
    survivor's
    stays (`policy_case` 3). Two Capabilities governed by different Policies are rejected before any
    approval, with an error naming both Policies and the `release-capability-governance` step; when
    neither Policy is a draft that step is unavailable and the error says there is no completion
    path
    (a Policy fork carries the whole governed set).

    Args:
        survivor_id: Id of the Capability that remains; at least 1 character.
        absorbed_id: Id of the Capability merged into the survivor; at least 1 character.
        acknowledge_governance_change: Set to `true` to accept the governance change of merge case
            2;
            default `false`. It has no effect on the other cases.
        ctx: Supplied by the server, not the client; used to build the base of `approval_url`.

    Returns:
        `{"preview": {"survivor_id", "survivor_name", "absorbed_id", "absorbed_name", "policy_case",
        "edges_to_move": {"requires", "covers", "mitigated_by"}, "duplicate_edges_collapsed",
        "obligations_affected", "state_digest", "governance"}, "pending_approval_id",
        "approval_url",
        "expires_at"}` (`governance` is `null` in case 1). For case 2 without the acknowledgment it
        returns `{"preview", "acknowledgment_required": true, "message"}` instead.

    Errors (returned as a string beginning `error: `, never raised; no approval is created on any
    error):
        - The caller is not an authenticated Compliance Officer.
        - A side is the same node as the other, does not exist, is a `merged` tombstone, or is not
          active.
        - The two Capabilities are governed by different Policies.
        - The policy graph database or the approval store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the policy graph and creates one pending approval row with an expiry and writes one
        log entry; the graph is not changed.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        actor = _gated_actor(config)
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


@_gated_tool("merge-obligations", _CLEANUP_GATE)
def merge_obligations_tool(
    survivor_id: Annotated[str, Field(min_length=1)],
    absorbed_id: Annotated[str, Field(min_length=1)],
    ctx: Context,
) -> dict[str, object] | str:
    """Preview merging one Obligation into another and request a passkey approval for the merge.

    Requires an explicit `ComplianceOfficer` grant on a real authenticated session (no admin
    override;
    never available under the local-test bypass). This call never edits the graph: it returns a
    preview
    and creates a pending passkey approval bound to this exact pair and to the state previewed. The
    merge runs only when the officer opens `approval_url` in a browser and signs with a passkey;
    `check-cleanup-approval` reports the outcome.

    Both Obligations must be borne by the same Role. On execution the absorbed Obligation's
    `SATISFIED_BY` and `REQUIRES` edges are unioned onto the survivor (no duplicate edges), the
    survivor
    keeps its single `HAS` Role edge, and the absorbed Obligation is deleted, with its full node and
    edge snapshot held in the audit row (`obligation.merge`). The change is one all-or-nothing graph
    write that is audited first. The deletion leaves a `MergedObligation` marker so that a later
    ingest
    or restore that regenerates the absorbed Obligation attaches to the survivor instead of
    recreating
    it.

    Args:
        survivor_id: Id of the Obligation that remains; at least 1 character.
        absorbed_id: Id of the Obligation merged into the survivor; at least 1 character.
        ctx: Supplied by the server, not the client; used to build the base of `approval_url`.

    Returns:
        `{"preview": {"survivor_id", "survivor_text", "absorbed_id", "absorbed_text", "role_id",
        "role_name", "edges_to_move": {"satisfied_by", "requires"}, "duplicate_edges_collapsed",
        "requirement_source_refs": [{"requirement_id", "source_ref"}], "state_digest"},
        "pending_approval_id", "approval_url", "expires_at"}`.

    Errors (returned as a string beginning `error: `, never raised; no approval is created on any
    error):
        - The caller is not an authenticated Compliance Officer.
        - A side is the same node as the other or does not exist.
        - The two Obligations are under different Roles.
        - The policy graph database or the approval store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the policy graph and creates one pending approval row with an expiry and writes one
        log entry; the graph is not changed.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        actor = _gated_actor(config)
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


@_gated_tool("release-capability-governance", _CLEANUP_GATE)
def release_capability_governance_tool(
    capability_id: Annotated[str, Field(min_length=1)],
    ctx: Context,
) -> dict[str, object] | str:
    """Preview releasing a Capability from its draft Policy and request a passkey approval.

    Requires an explicit `ComplianceOfficer` grant on a real authenticated session (no admin
    override;
    never available under the local-test bypass). This call never edits the graph: it returns a
    preview
    and creates a pending passkey approval bound to this Capability, its governing Policy and the
    state
    previewed. The release runs only when the officer opens `approval_url` in a browser and signs
    with a
    passkey; `check-cleanup-approval` reports the outcome.

    Only a Capability governed by a `draft` Policy can be released. On execution its single
    `GOVERNED_BY`
    edge to that Policy is deleted: the Capability and the Policy stay and the Capability becomes
    ungoverned. The change is one all-or-nothing graph write that is audited first
    (`capability.release_governance`, with a before and after snapshot). A `proposed` Policy is
    rejected
    with a pointer to `revert-policy-to-draft`; an `approved` or `deprecated` Policy is rejected
    with a
    pointer to the Policy lifecycle, stating that a fork carries the whole governed set and so does
    not
    by itself free the Capability.

    Args:
        capability_id: Id of the Capability to release; at least 1 character.
        ctx: Supplied by the server, not the client; used to build the base of `approval_url`.

    Returns:
        `{"preview": {"capability_id", "capability_name", "policy_id", "policy_title",
        "policy_status",
        "governed_set_before", "governed_set_after", "state_digest"}, "pending_approval_id",
        "approval_url", "expires_at"}`.

    Errors (returned as a string beginning `error: `, never raised; no approval is created on any
    error):
        - The caller is not an authenticated Compliance Officer.
        - The Capability does not exist, is a `merged` tombstone, is not active, or is not governed.
        - Its governing Policy is not a draft.
        - The policy graph database or the approval store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the policy graph and creates one pending approval row with an expiry and writes one
        log entry; the graph is not changed.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        actor = _gated_actor(config)
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


@_gated_tool("unmerge", _CLEANUP_GATE)
def unmerge_tool(
    merged_id: Annotated[str, Field(min_length=1)],
    ctx: Context,
) -> dict[str, object] | str:
    """Preview reversing a Compliance Officer merge and request a passkey approval for the reversal.

    Requires an explicit `ComplianceOfficer` grant on a real authenticated session (no admin
    override;
    never available under the local-test bypass). The merge is found in the audit trail
    (`capability.merge` or `obligation.merge`): the newest `applied` row not followed by a `failed`
    row
    for the same approval. This call never edits the graph: it returns a preview and creates a
    pending
    passkey approval bound to the merge it reverses and to the state previewed. The unmerge runs
    only
    when the officer opens `approval_url` in a browser and signs with a passkey;
    `check-cleanup-approval`
    reports the outcome.

    Capability merge: the tombstone returns to `active` with its `MERGED_INTO` edge removed, and
    exactly
    the edges the merge moved off it are restored from the audit snapshot (the survivor keeps any
    edge it
    already had before the merge). Edges added to the survivor since the merge stay in place and are
    listed as `survivor_added_edges`.

    Obligation merge: the deleted Obligation is recreated under its original id with its properties,
    its
    `HAS` edge from the Role and exactly its `SATISFIED_BY` and `REQUIRES` edges from the audit
    snapshot,
    and its `MergedObligation` marker is removed. The survivor's edges are never removed, because a
    union
    cannot be attributed: edges added since the merge are listed as `survivor_added_edges`, and
    those
    that may have come from the absorbed Obligation as `survivor_edges_possibly_from_merge`.

    Either way the change is one all-or-nothing graph write that is audited first
    (`capability.unmerge`
    or `obligation.unmerge`, with a before and after snapshot).

    Args:
        merged_id: Id absorbed by `merge-capabilities` (a `merged` Capability tombstone) or by
            `merge-obligations` (a deleted Obligation); at least 1 character.
        ctx: Supplied by the server, not the client; used to build the base of `approval_url`.

    Returns:
        `{"preview": {"kind": "capability" | "obligation", "merged_id", "survivor_id",
        "merge_approval_id", "edges_to_restore", "survivor_added_edges", "state_digest", ...},
        "pending_approval_id", "approval_url", "expires_at"}`. A capability preview also carries
        `merged_name`, `survivor_name` and `edges_removed_from_survivor`; an obligation preview
        carries
        `merged_text`, `survivor_text`, `role_id`, `survivor_edges_possibly_from_merge` and `note`.

    Errors (returned as a string beginning `error: `, never raised; no approval is created on any
    error):
        - The caller is not an authenticated Compliance Officer.
        - No merge of the id is in the audit trail.
        - The merge cannot be reversed because of a conflict (the survivor was merged away or
          re-pointed,
          its governance changed, a restored edge's endpoint is gone, or the Obligation exists
          again);
          the message explains which.
        - The audit trail, the policy graph database or the approval store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the audit trail and the policy graph and creates one pending approval row with an
        expiry;
        the graph is not changed.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        actor = _gated_actor(config)
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


@_gated_tool("check-cleanup-approval", _CLEANUP_GATE)
def check_cleanup_approval_tool(
    pending_approval_id: Annotated[str, Field(min_length=1)],
) -> dict[str, object] | str:
    """Report the status and outcome of a graph-cleanup passkey approval.

    Companion to `merge-capabilities`, `merge-obligations`, `release-capability-governance` and
    `unmerge`: the passkey ceremony happens in a browser, so this is how to learn whether the
    approval
    was signed and what the edit did. Requires an explicit `ComplianceOfficer` grant. Only the
    officer
    who created the approval can see it; any other caller, or an unknown id, gets the same not-found
    error.

    Args:
        pending_approval_id: The `pending_approval_id` returned by the tool that created the
            approval;
            at least 1 character.

    Returns:
        `{"pending_approval_id", "status", "tool_name", "outcome"}`. `status` is `pending`,
        `expired`
        (the window elapsed unsigned, derived live) or `signed`. `outcome` is `null` until the
        signed
        approval has run, then one of: the result (`{"survivor_id", "absorbed_id", "merged": true}`
        for
        either merge tool, `{"capability_id", "policy_id", "released": true}` for a release,
        `{"merged_id", "survivor_id", "unmerged": true, "survivor_added_edges"}` for an unmerge of
        either
        kind), `{"error": <message>}`, or `{"reconciled": "applied"}`. An `applied` audit row
        followed by
        a `failed` row for the same approval id means no edit occurred.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller is not an authenticated Compliance Officer.
        - No such approval is visible to the caller.
        - The approval status could not be settled right now (the graph or audit trail is
          unreachable).
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        A signed approval whose outcome was never recorded is checked against the graph once it is
        more
        than five minutes past its expiry, and settled: if the edit is present the outcome becomes
        `reconciled`; if not, a `failed` audit row is written and the outcome becomes an error.
        Otherwise nothing is written.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        actor = _gated_actor(config)
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
