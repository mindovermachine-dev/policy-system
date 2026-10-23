"""Ingestion and regulation-change tools of the PS MCP server.

Registers `ingest_regulation`, `start_ingestion`, `get_ingestion_status`, `check_regulations` and
`check_instrument_ingestion_status` on the shared `server` from `tool_core`, together with the
background ingestion-run machinery behind `start_ingestion`. This module imports only `tool_core`
(never another tool group); the patch seam names are looked up there at call time as
`tool_core.<Name>`. `build_default_pipeline_dependencies`,
`build_default_change_check_dependencies`, `PsycopgIngestionRunStore`, `start_background_run` and
`run_mcp_action` are bound here and patched here by tests.

Business Rules:
    - Ingestion and change-check tools need `ComplianceOfficer`; ingestion status reads and the
      instrument ingestion-status check are plain reads.
    - A tool failure crosses the MCP boundary as an `error: <message>` string with fixed,
      detail-free wording for infrastructure failures.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field

from ps_service import dependency_health
from ps_service.api.change_check_orchestration import (
    build_default_change_check_dependencies,
    run_change_check_sweep,
)
from ps_service.api.errors import (
    CatalogIdentifierNotFoundError,
    CelexAlreadyIngestedError,
    GraphWriteUnavailableError,
    IngestionConfigIncompleteError,
    PipelineStageError,
    ShortNameCollisionError,
)
from ps_service.api.ingestion_orchestration import (
    SHORT_NAME_PATTERN as _SHORT_NAME_PATTERN,  # shared with api/models.py's short_name
)
from ps_service.api.ingestion_orchestration import (
    GraphOpeners,
    _require_ingestion_config,  # pyright: ignore[reportPrivateUsage]  -- the identical config-completeness check, reused so the error text can never drift
    build_default_pipeline_dependencies,
    classify_ingestion_failure,
    resolve_ingestion_entry,
    run_audited_catalog_ingestion,
    run_catalog_ingestion_pipeline,
)
from ps_service.api.models import IngestionRunStatusResponse, StartIngestionResponse
from ps_service.api.routes import (
    _to_accepted_response,  # pyright: ignore[reportPrivateUsage]  -- reuses the REST wire-shaping helper verbatim so the MCP and REST paths can never silently drift; mirrors change_check_orchestration.py's cross-module private-import convention
    _to_change_check_response,  # pyright: ignore[reportPrivateUsage]  -- same reuse for `check_regulations`, mirroring `_to_accepted_response` immediately above
)
from ps_service.api.run_status import get_stage
from ps_service.api.stage_failure_log import stage_failure_detail
from ps_service.audit.errors import AuditTrailUnavailableError
from ps_service.config import load_config
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
from ps_service.logging import LoggingLifecycleError, current_run_id, emit_log_entry
from ps_service.mcp_interface import tool_core
from ps_service.mcp_interface.errors import McpGraphUnavailableError
from ps_service.mcp_interface.tool_core import CELEX_PATTERN as _CELEX_PATTERN
from ps_service.mcp_interface.tool_core import COMPLIANCE_OFFICER_GATE as _COMPLIANCE_OFFICER_GATE
from ps_service.mcp_interface.tool_core import COMPONENT as _COMPONENT
from ps_service.mcp_interface.tool_core import (
    GRAPH_UNAVAILABLE_MESSAGE as _GRAPH_UNAVAILABLE_MESSAGE,
)
from ps_service.mcp_interface.tool_core import MCP_CALLER_LABEL as _MCP_CALLER_LABEL
from ps_service.mcp_interface.tool_core import PLAIN_READ_GATE as _PLAIN_READ_GATE
from ps_service.mcp_interface.tool_core import UNEXPECTED_ERROR_MESSAGE as _UNEXPECTED_ERROR_MESSAGE
from ps_service.mcp_interface.tool_core import audit_context as _audit_context
from ps_service.mcp_interface.tool_core import gated_server_tool as _gated_tool
from ps_service.mcp_interface.tool_core import resolve_authz_actor as _resolve_authz_actor
from ps_service.mcp_interface.tool_core import resolve_graph as _resolve_graph
from ps_service.mcp_interface.tool_core import run_mcp_action as _run_mcp_action
from ps_service.mcp_interface.tool_core import sanitize_graph_open as _sanitize_graph_open
from ps_service.metrics import record_ingestion_run
from ps_service.query_engine import (
    GraphUnseededError,
    QueryEngineExecutionError,
    WriteClauseRejectedError,
    execute_cypher_query,
)

if TYPE_CHECKING:
    from ps_service.api.catalog_entry import CatalogEntry
    from ps_service.api.change_check_orchestration import ChangeCheckDependencies
    from ps_service.api.ingestion_orchestration import PipelineDependencies
    from ps_service.audit.emit import AuditContext
    from ps_service.config import ServiceConfig
    from ps_service.ingestion.adapters.base import IngestionAdapter
    from ps_service.ingestion_runs import IngestionRunRow, IngestionRunStore
    from ps_service.ingestion_runs.audit_actions import IngestionReasonCode, IngestionTrigger


# `_SHORT_NAME_PATTERN` is `SHORT_NAME_PATTERN` from `ps_service.api.ingestion_orchestration`:
# a letter first, then alphanumerics, `_` or `-`, 1-64 characters in all (as "cra", "gdpr" and
# "nis2"). It is one pattern literal shared with `api/models.py`'s
# `CatalogIngestionRequest.short_name`, so REST and MCP accept the same names.

# Same wording ps-cli uses for this condition, so both client paths read alike to operators.
_LLM_INTERFACE_UNAVAILABLE_MESSAGE = "error: LLM Interface is unavailable."


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
            caller=_MCP_CALLER_LABEL,
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
            caller=_MCP_CALLER_LABEL,
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
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        if not dependency_health.is_healthy(dependency_health.LLM_INTERFACE):
            return _LLM_INTERFACE_UNAVAILABLE_MESSAGE
        # `_run_mcp_action` always binds a run_id via `bind_run_context()` before
        # calling this closure, so `current_run_id()` is never actually `None`
        # here -- the `""` fallback only satisfies the type checker's narrowing.
        run_id = current_run_id() or ""
        audit = _audit_context(config, actor)
        if isinstance(audit, str):
            return audit
        return _resolve_and_ingest(celex, short_name, config=config, run_id=run_id, audit=audit)

    return _run_mcp_action("ingest_regulation", _body)


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
    duration_seconds: float,
) -> None:
    """Write the run's one terminal row: `failed` for an `error:` string, else `succeeded`.

    `reason_code` is the enumerated cause of a failure (`unexpected_error` when no known branch
    recorded one); the `error:` text goes to the run row only, never to the audit row.

    The write is a single-winner compare-and-swap, so a row is never terminal twice. Only the
    call that won it records the run's outcome and `duration_seconds` as metrics.
    A failed write is logged (exception class only, never host detail) and swallowed: nothing
    may escape the worker unlogged, and the row simply stays `running`.
    """
    try:
        if isinstance(result, str):
            won = store.complete_run(
                run_id,
                status="failed",
                result=None,
                error=result,
                reason_code=reason_code or "unexpected_error",
            )
            outcome: Literal["succeeded", "failed"] = "failed"
        else:
            won = store.complete_run(run_id, status="succeeded", result=result, error=None)
            outcome = "succeeded"
        if won:
            record_ingestion_run(outcome, duration_seconds)
    except Exception as exc:  # noqa: BLE001 -- nothing may escape the worker unlogged; the row stays `running`
        _emit_best_effort_failure(_BACKGROUND_INGESTION_ACTION, run_id, reason=type(exc).__name__)


def _execute_background_ingestion(
    prepared: _PreparedCatalogIngestion,
    *,
    run_id: str,
    config: ServiceConfig,
    store: IngestionRunStore,
) -> None:
    """Run the prepared pipeline on the worker thread, then record its outcome.

    The pipeline call goes through `_run_mcp_action` so the run gets its own correlated
    started/succeeded/failed log lines under the submitted `run_id` and every unexpected
    exception is sanitized exactly as for a blocking tool call. The outer `except` is the
    last-resort net: the run's row must never be left `running` by an exception.
    """
    capture = _FailureCapture()
    started = time.monotonic()
    try:
        result = _run_mcp_action(
            _BACKGROUND_INGESTION_ACTION,
            lambda: _run_prepared_catalog_ingestion(
                prepared,
                config=config,
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
            exception_type=type(exc).__name__,
            detail=stage_failure_detail(exc),
        )
        result = _UNEXPECTED_ERROR_MESSAGE
    _record_ingestion_run_outcome(
        store,
        run_id=run_id,
        result=result,
        reason_code=capture.reason_code,
        duration_seconds=time.monotonic() - started,
    )


def _reconcile_orphaned_run(store: IngestionRunStore, row: IngestionRunRow) -> IngestionRunRow:
    """Mark a `running` row no in-process worker holds as failed (interrupted), then re-read it.

    A declared, idempotent side effect of `get_ingestion_status`: the row is
    `running` yet its worker is gone (its terminal write failed, or the process restarted), so
    it would otherwise be reported `running` forever. The write is a single-winner
    compare-and-swap, a no-op if the worker finished first. It never fails the poll: any store
    error is logged (exception class only) and the row is returned unchanged. The call that wins
    the write records one `failed` run, aged from the row's submission time.
    """
    if row.status != "running" or is_run_in_flight(row.run_id):
        return row
    try:
        won = store.complete_run(
            row.run_id,
            status="failed",
            result=None,
            error=_INTERRUPTED_RUN_MESSAGE,
            reason_code="interrupted",
            audit_actor=_RECONCILER_AUDIT_ACTOR,
        )
        if won:
            age = (datetime.now(UTC) - row.submitted_at).total_seconds()
            record_ingestion_run("failed", age)
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
            actor=actor,
            store=PsycopgIngestionRunStore(config, audit_store=tool_core.PsycopgAuditStore(config)),
        )

    return _run_mcp_action("start_ingestion", _body)


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

    def _body() -> dict[str, object] | str:
        store = PsycopgIngestionRunStore(config, audit_store=tool_core.PsycopgAuditStore(config))
        try:
            row = store.get_run(run_id)
        except IngestionRunStoreError as exc:
            return f"error: {exc}"
        if row is not None:
            row = _reconcile_orphaned_run(store, row)
        return _ingestion_run_status_response(run_id, row)

    return _run_mcp_action("get_ingestion_status", _body)


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
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        if not dependency_health.is_healthy(dependency_health.LLM_INTERFACE):
            return _LLM_INTERFACE_UNAVAILABLE_MESSAGE
        # `_run_mcp_action` always binds a run_id via `bind_run_context()` before
        # calling this closure, so `current_run_id()` is never actually `None`
        # here -- the `""` fallback only satisfies the type checker's narrowing,
        # mirroring `ingest_regulation`'s own identical idiom.
        return _run_audited_change_check(config, actor, current_run_id() or "")

    return _run_mcp_action("check_regulations", _body)


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

    def _body() -> dict[str, object]:
        try:
            graph = _resolve_graph(config)
            result = execute_cypher_query(
                _INGESTION_STATUS_QUERY,
                graph=graph,
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

    return _run_mcp_action("check_instrument_ingestion_status", _body)
