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

import dataclasses
import functools
import os
from importlib import resources
from typing import TYPE_CHECKING, Annotated, Literal

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.resolve import Elicit, Resolve
from pydantic import AfterValidator, BaseModel, Field

from ps_service import dependency_health
from ps_service.api.catalog import find_by_celex
from ps_service.api.change_check_orchestration import (
    build_default_change_check_dependencies,
    run_change_check_sweep,
)
from ps_service.api.errors import (
    CatalogIdentifierNotFoundError,
    CuratedSourceUnavailableError,
    IngestionConfigIncompleteError,
    PendingReviewNotFoundError,
    PipelineStageError,
    RestoreArtifactRejectedError,
    RestoreStageFailedError,
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
from ps_service.config import LOCAL_TEST_PRINCIPAL_ID, ServiceConfigurationError, load_config
from ps_service.curated_source import store as catalog_source_store
from ps_service.curated_source.catalog_client import build_default_curated_catalog_dependencies
from ps_service.curated_source.errors import (
    CuratedSourceConfigurationError,
    CuratedSourceFetchError,
)
from ps_service.curated_source.resolve import resolve_effective_source
from ps_service.curated_source.source_url import validate_source_url
from ps_service.logging import bind_run_context, current_run_id, emit_log_entry
from ps_service.mcp_interface.errors import (
    McpGraphUnavailableError,
    McpResourceUnavailableError,
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

if TYPE_CHECKING:
    from collections.abc import Callable
    from importlib.resources.abc import Traversable

    from ps_service.api.catalog import CatalogEntry
    from ps_service.api.change_check_orchestration import ChangeCheckDependencies
    from ps_service.api.ingestion_orchestration import PipelineDependencies
    from ps_service.api.near_miss_review_orchestration import NearMissReviewDependencies
    from ps_service.api.restore_orchestration import CatalogRestoreDependencies
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


# CHANGES.md H2: the merge-decision confirmation gate's own warning text --
# both the resolver's `Elicit(...)` message the client actually sees and the
# secondary signal repeated in `near_misses_resolve`'s own docstring (same
# sentence, per D-MERGE-WARN point 2), so the two channels never drift.
_MERGE_WARNING = (
    'WARNING: decision="merge" is IRREVERSIBLE -- it deletes the loser node and '
    "re-points every edge that referenced it onto the winner, atomically, before "
    'this call returns. Reply with confirm="merge" to proceed.'
)


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


class MergeConfirmation(BaseModel):
    """CHANGES.md H2: the confirmation payload a client must supply to proceed with a merge."""

    confirm: Literal["merge"]


def _confirm_merge(decision: str) -> Elicit[MergeConfirmation] | bool:
    """CHANGES.md H2: `near_misses_resolve`'s merge-confirmation resolver.

    Runs before the tool body, given the tool's own already-validated
    `decision` argument by name (the SDK's resolver-DAG wiring, not a
    manual lookup). `decision="keep-separate"` resolves instantly with no
    elicitation round trip -- Slice 3.2's existing single-round-trip
    behavior for that branch is unchanged. `decision="merge"` returns an
    `Elicit` marker instead: the SDK sends (or, on the >= 2026-07-28
    protocol, batches into an `InputRequiredResult`) an `elicitation/create`
    request carrying `_MERGE_WARNING`, and does not call this tool's body
    until the client answers. A decline/cancel answer raises `ToolError`
    automatically (the resolver's own consumer -- `near_misses_resolve`'s
    `confirmed` parameter -- is annotated to receive the unwrapped value,
    not the full outcome union), so no merge write ever happens on that
    path either.
    """
    if decision != "merge":
        return True
    return Elicit(message=_MERGE_WARNING, schema=MergeConfirmation)


@server.tool()
def near_misses_resolve(
    review_id: Annotated[str, Field(min_length=1)],
    decision: Literal["keep-separate", "merge"],
    confirmed: Annotated[bool | MergeConfirmation, Resolve(_confirm_merge)],
) -> dict[str, object] | str:
    """ResolveNearMiss: resolve one near-miss pending review.

    Runs in-process, exactly like `POST /near-misses/{review_id}/resolve`
    does: `decision="keep-separate"` deletes only the `PendingReview`
    record -- `winner_id`/`loser_id` stay `None` in the response (this
    slice's own implemented happy path). `decision`'s `Literal` type is
    itself the MCP-schema-level rejection of any other value, before this
    tool's body ever runs.

    WARNING: decision="merge" is IRREVERSIBLE -- it deletes the loser node
    and re-points every edge that referenced it onto the winner,
    atomically, before this call returns. Reply with confirm="merge" to
    proceed. This is also the exact message a real elicitation round trip
    sends before any merge write happens (CHANGES.md H2, reversing
    D-MERGE-WARN's earlier rejection of this mechanism): calling with
    decision="merge" pauses the call until the client answers with
    `{"confirm": "merge"}`; a decline or cancel answer aborts the call
    before any write; and a client that has not declared the elicitation
    capability gets a clear protocol error instead of an unconfirmed merge.
    `decision="keep-separate"` never pauses -- one round trip, exactly as
    before.

    Like `near_misses_list`, this tool has no LLM Interface dependency of
    its own and so runs no LLM-Interface pre-flight check.

    On success, returns the same structured summary `POST
    /near-misses/{review_id}/resolve` returns: `review_id`, `decision`, and
    `winner_id`/`loser_id` (populated for `merge`, `None` for
    `keep-separate`). Returns a string beginning `error: ` when `review_id`
    doesn't exist or was already resolved, when (`merge` only) it references
    a node a prior merge already deleted (a stale reference), when the
    policy graph database cannot be reached, or (this tool's own residual
    safety net) on any other unexpected failure.
    """
    # `confirmed` is a resolver-filled gate (CHANGES.md H2): its presence on
    # the signature is what forces the elicitation round-trip for `merge`
    # before this body ever runs; the value itself carries nothing further
    # this body needs (the tool never reaches here on a decline/cancel).
    _ = confirmed
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
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

    On success, returns `{"url": <the validated url>, "source": "override"}`.
    Returns a string beginning `error: ` when `url` fails validation, when
    the policy graph database cannot be reached, or (this tool's own
    residual safety net) on any other unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
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

    On success, returns `{"url": <the env-var/default url>, "source": "default"}`.
    Returns a string beginning `error: ` when the policy graph database
    cannot be reached, or (this tool's own residual safety net) on any other
    unexpected failure.
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
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

    On success, returns `{"url": <the effective url>, "source": "override"}`
    when a persisted override is in effect, or `{"url": ..., "source":
    "default"}` otherwise. Returns a string beginning `error: ` only on this
    tool's own residual safety net (an unexpected failure unrelated to the
    FalkorDB override check, which always fails open rather than erroring).
    """
    config = load_config()
    principal = _resolve_principal(config)

    def _body() -> dict[str, object] | str:
        effective = resolve_effective_source(config, open_graph=lambda: _resolve_graph(config))
        return {"url": effective.url, "source": "override" if effective.is_override else "default"}

    return _run_mcp_action("get_catalog_source", principal, _body)


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
