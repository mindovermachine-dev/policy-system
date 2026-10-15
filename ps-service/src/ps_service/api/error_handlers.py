"""FastAPI exception handlers for the PS Service REST API.

Every 4xx/5xx body this API returns is minted here, in one shape
(``{"error": {"code", "message", "failing_stage"}, "run_id": ...}``). Messages from a
whitelisted set of domain-level exception types are surfaced verbatim after path/host
scrubbing; every other exception collapses to a generic message, with the real detail
written, scrubbed, to the operator log only. No stack trace, filesystem path, repo/home
directory, ``host:port`` token, or URL ever reaches a response body.

Public entry points: ``register_exception_handlers`` installs every handler on an app, and
``is_safe_verbatim`` tells whether an exception's message may be shown to a caller.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ps_service.api.errors import (
    AccessDeniedError,
    AuthorizationStoreUnavailableError,
    CatalogIdentifierNotFoundError,
    CatalogSourceOverrideUnavailableError,
    CelexAlreadyIngestedError,
    CuratedSourceUnavailableError,
    ExportConfigIncompleteError,
    ExportInstrumentNotFoundError,
    ExportStageFailedError,
    GraphWriteUnavailableError,
    IngestionConfigIncompleteError,
    InternalIngestOwnerRequiredError,
    InternalSeedValidationError,
    InvalidAccessRoleError,
    InvalidAuditQueryFilterError,
    MergeApprovalRequiresAuthenticatedCallerError,
    PendingApprovalInvalidOrExpiredError,
    PendingApprovalNotFoundError,
    PendingReviewNotFoundError,
    PipelineStageError,
    RequestBodyTooLargeError,
    RestoreArtifactRejectedError,
    RestoreInstrumentIdAmbiguousError,
    RestoreInstrumentIdNotFoundError,
    RestoreStageFailedError,
    SelfGrantOrRevokeBlockedError,
    ShortNameCollisionError,
    SystemOwnerFloorViolationError,
)
from ps_service.audit.errors import AuditTrailUnavailableError
from ps_service.logging.facade import emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import FastAPI, Request

_LOG = logging.getLogger(__name__)

# --- text scrubbing ----------------------------------------------------------

_REPO_ROOT: str = str(Path(__file__).resolve().parents[4])
_HOME_DIR: str = str(Path.home())

_TRAILING_PATH = r"[\w./\-]*"
_REPO_ROOT_RE = re.compile(re.escape(_REPO_ROOT) + _TRAILING_PATH)
_HOME_DIR_RE = re.compile(re.escape(_HOME_DIR) + _TRAILING_PATH)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_HOST_PORT_RE = re.compile(r"\b[\w.\-]+:\d{2,5}\b")
_ABS_PATH_RE = re.compile(r"(?:/[\w.\-]+){2,}/?")
_HOME_TILDE_RE = re.compile(r"~(?:/[\w.\-]+)*")
_ENV_HOME_RE = re.compile(r"\$HOME(?:/[\w.\-]+)*")

_PATH_PLACEHOLDER = "[redacted-path]"
_URL_PLACEHOLDER = "[redacted-url]"
_ADDR_PLACEHOLDER = "[redacted-addr]"
_REASON_MAX_LEN = 300


def _scrub_text(text: str) -> str:
    """Strip filesystem paths, the repo/home dirs, ``host:port`` tokens, and URLs.

    Applied to every message surfaced to a caller or written to the operator log, so a detail
    string cannot reveal where the service runs. Regexes are bounded (a single character-class
    quantifier each, no nested quantifiers) so no input causes catastrophic backtracking.

    Args:
        text: The raw message; any string, including empty.

    Returns:
        The message with each repo-root, home-directory, URL, ``host:port``, absolute-path,
        ``~`` and ``$HOME`` span replaced by a fixed ``[redacted-...]`` placeholder. Text with
        no such span is returned unchanged.
    """
    scrubbed = _REPO_ROOT_RE.sub(_PATH_PLACEHOLDER, text)
    scrubbed = _HOME_DIR_RE.sub(_PATH_PLACEHOLDER, scrubbed)
    scrubbed = _URL_RE.sub(_URL_PLACEHOLDER, scrubbed)
    scrubbed = _HOST_PORT_RE.sub(_ADDR_PLACEHOLDER, scrubbed)
    scrubbed = _ABS_PATH_RE.sub(_PATH_PLACEHOLDER, scrubbed)
    scrubbed = _HOME_TILDE_RE.sub(_PATH_PLACEHOLDER, scrubbed)
    return _ENV_HOME_RE.sub(_PATH_PLACEHOLDER, scrubbed)


# --- safe-verbatim whitelist -------------------------------------------------

_SAFE_VERBATIM: tuple[type[Exception], ...] = (
    AuditTrailUnavailableError,
    AccessDeniedError,
    AuthorizationStoreUnavailableError,
    CelexAlreadyIngestedError,
    CatalogIdentifierNotFoundError,
    CatalogSourceOverrideUnavailableError,
    InternalSeedValidationError,
    GraphWriteUnavailableError,
    IngestionConfigIncompleteError,
    RequestBodyTooLargeError,
    RestoreArtifactRejectedError,
    RestoreInstrumentIdAmbiguousError,
    RestoreInstrumentIdNotFoundError,
    PendingReviewNotFoundError,
    ExportInstrumentNotFoundError,
    ExportConfigIncompleteError,
    CuratedSourceUnavailableError,
    MergeApprovalRequiresAuthenticatedCallerError,
    InternalIngestOwnerRequiredError,
    PendingApprovalNotFoundError,
    PendingApprovalInvalidOrExpiredError,
    InvalidAccessRoleError,
    SelfGrantOrRevokeBlockedError,
    SystemOwnerFloorViolationError,
    ShortNameCollisionError,
)
"""API-boundary error types whose ``str(exc)`` is domain-level and safe to surface."""

_SAFE_VERBATIM_NAMES: frozenset[str] = frozenset(
    {
        "CatalogIdentifierNotFoundError",
        "InternalSeedValidationError",
        "IngestionConfigIncompleteError",
        "InternalSeedError",
        "DomainMapperExtractionError",
        "DomainMapperDerivationError",
        "CompanyMergeConfigurationError",
        "ArtifactContentRejectedError",  # ps_service.restore -- by name, never imported
        "GovernanceOwnerRequiredError",  # ps_service.company_merge -- by name
        "RestoreOwnerRequiredError",  # ps_service.restore -- by name, never imported
    }
)
"""Whitelisted domain-error class names, matched by name so this module never
imports ``ps_service.domain_mapper`` / ``ps_service.company_merge`` /
``ps_service.restore`` at load time (layering + the Process Harness's
no-transitive-pipeline-import guarantee)."""


def is_safe_verbatim(exc: BaseException) -> bool:
    """Return whether ``str(exc)`` may be surfaced to the caller (after scrubbing).

    Domain-level messages are safe to show; anything else (driver, network, library errors)
    can embed addresses, paths or credentials and is treated as detail-leaking. Used by the
    verbatim handlers here and by the ingestion orchestration when it builds the reason of a
    ``PipelineStageError``.

    Business Rules:
        True for the API-boundary types in the whitelist and, matched by class name, for the
        whitelisted Domain Mapper, Company Merge, Ingestion and Restore domain errors. Matching
        by name keeps this module from importing those packages. Everything else is False.

    Args:
        exc: The exception to classify.

    Returns:
        ``True`` if the exception's message is domain-level and caller-safe, else ``False``.
        Never raises.
    """
    return isinstance(exc, _SAFE_VERBATIM) or type(exc).__name__ in _SAFE_VERBATIM_NAMES


# --- body minting ------------------------------------------------------------

_GENERIC_500_CODE = "internal_error"
_GENERIC_500_MESSAGE = "An internal error occurred."
_VALIDATION_CODE = "validation_error"
_VALIDATION_MESSAGE = "Request validation failed."


def _error_body(
    *,
    code: str,
    message: str,
    run_id: str | None,
    failing_stage: str | None = None,
) -> dict[str, object]:
    """Mint the single error-body shape this API returns.

    Args:
        code: A stable machine-readable error code.
        message: A caller-safe, already-scrubbed message.
        run_id: The request's run id, or ``None`` if none was bound.
        failing_stage: The failing pipeline stage, set only for stage-failure errors; ``None``
            otherwise.

    Returns:
        ``{"error": {"code", "message", "failing_stage"}, "run_id": ...}``.
    """
    return {
        "error": {"code": code, "message": message, "failing_stage": failing_stage},
        "run_id": run_id,
    }


def _bound_run_id(request: Request) -> str | None:
    """Return the run id bound on ``request.state`` by ``provide_run_id``, or ``None``."""
    run_id: object = getattr(request.state, "run_id", None)
    return run_id if isinstance(run_id, str) else None


def _json(status_code: int, body: dict[str, object]) -> JSONResponse:
    """Wrap ``body`` in a ``JSONResponse`` with the given status code."""
    return JSONResponse(status_code=status_code, content=body)


# --- handlers ---------------------------------------------------------------

_API_ERROR_SPECS: tuple[tuple[type[Exception], str, int], ...] = (
    (
        AuditTrailUnavailableError,
        "audit_trail_unavailable",
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
    (AccessDeniedError, "access_denied", status.HTTP_403_FORBIDDEN),
    (
        AuthorizationStoreUnavailableError,
        "authorization_store_unavailable",
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
    (CatalogIdentifierNotFoundError, "catalog_identifier_not_found", status.HTTP_404_NOT_FOUND),
    (
        CatalogSourceOverrideUnavailableError,
        "catalog_source_override_unavailable",
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
    (InternalSeedValidationError, "internal_seed_invalid", status.HTTP_422_UNPROCESSABLE_CONTENT),
    (
        IngestionConfigIncompleteError,
        "ingestion_config_incomplete",
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
    (
        GraphWriteUnavailableError,
        "graph_write_unavailable",
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
    (
        RestoreArtifactRejectedError,
        "restore_artifact_rejected",
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    ),
    (
        RequestBodyTooLargeError,
        "request_body_too_large",
        status.HTTP_413_CONTENT_TOO_LARGE,
    ),
    (
        PendingReviewNotFoundError,
        "pending_review_not_found",
        status.HTTP_404_NOT_FOUND,
    ),
    (
        ExportInstrumentNotFoundError,
        "export_instrument_not_found",
        status.HTTP_404_NOT_FOUND,
    ),
    (
        ExportConfigIncompleteError,
        "export_config_incomplete",
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
    (
        CuratedSourceUnavailableError,
        "curated_source_unavailable",
        status.HTTP_502_BAD_GATEWAY,
    ),
    (
        MergeApprovalRequiresAuthenticatedCallerError,
        "merge_approval_requires_authenticated_caller",
        status.HTTP_401_UNAUTHORIZED,
    ),
    (
        InternalIngestOwnerRequiredError,
        "owner_required",
        status.HTTP_401_UNAUTHORIZED,
    ),
    (
        PendingApprovalNotFoundError,
        "pending_approval_not_found",
        status.HTTP_404_NOT_FOUND,
    ),
    (
        PendingApprovalInvalidOrExpiredError,
        "pending_approval_invalid_or_expired",
        status.HTTP_404_NOT_FOUND,
    ),
    (
        InvalidAccessRoleError,
        "invalid_access_role",
        status.HTTP_400_BAD_REQUEST,
    ),
    (
        SelfGrantOrRevokeBlockedError,
        "self_grant_revoke_blocked",
        status.HTTP_403_FORBIDDEN,
    ),
    (
        SystemOwnerFloorViolationError,
        "system_owner_floor_violation",
        status.HTTP_403_FORBIDDEN,
    ),
    (
        InvalidAuditQueryFilterError,
        "invalid_audit_query_filter",
        status.HTTP_400_BAD_REQUEST,
    ),
    (
        ShortNameCollisionError,
        "short_name_collision",
        status.HTTP_409_CONFLICT,
    ),
    (
        CelexAlreadyIngestedError,
        "celex_already_ingested",
        status.HTTP_409_CONFLICT,
    ),
    (
        RestoreInstrumentIdAmbiguousError,
        "restore_instrument_id_ambiguous",
        status.HTTP_409_CONFLICT,
    ),
    (
        RestoreInstrumentIdNotFoundError,
        "restore_instrument_id_not_found",
        status.HTTP_404_NOT_FOUND,
    ),
)


def _make_verbatim_handler(
    code: str, status_code: int
) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
    """Build an exception handler returning ``code`` and ``status_code`` for one error type.

    The handler shows the scrubbed ``str(exc)`` when ``is_safe_verbatim`` accepts it, else the
    generic internal-error message under the same code and status.
    """

    async def _handler(request: Request, exc: Exception) -> JSONResponse:
        message = _scrub_text(str(exc)) if is_safe_verbatim(exc) else _GENERIC_500_MESSAGE
        return _json(
            status_code,
            _error_body(code=code, message=message, run_id=_bound_run_id(request)),
        )

    return _handler


async def _handle_pipeline_stage_error(request: Request, exc: Exception) -> JSONResponse:
    """Shape a ``PipelineStageError`` body: HTTP 502, naming the failing stage.

    The message is the scrubbed stage reason cut to 300 characters. If ``exc`` is not a
    ``PipelineStageError`` (not expected: registered for that type only) a generic 500 is
    returned.
    """
    run_id = _bound_run_id(request)
    if not isinstance(exc, PipelineStageError):  # defensive — registered for this type only
        return _json(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            _error_body(code=_GENERIC_500_CODE, message=_GENERIC_500_MESSAGE, run_id=run_id),
        )
    return _json(
        status.HTTP_502_BAD_GATEWAY,
        _error_body(
            code="pipeline_stage_failed",
            message=_scrub_text(exc.reason)[:_REASON_MAX_LEN],
            run_id=run_id,
            failing_stage=exc.stage,
        ),
    )


async def _handle_restore_stage_failed_error(request: Request, exc: Exception) -> JSONResponse:
    """Shape a ``RestoreStageFailedError`` body: HTTP 502, naming the failing stage.

    The message is the scrubbed stage reason cut to 300 characters. If ``exc`` is not a
    ``RestoreStageFailedError`` (not expected: registered for that type only) a generic 500 is
    returned.
    """
    run_id = _bound_run_id(request)
    if not isinstance(exc, RestoreStageFailedError):  # defensive — registered for this type only
        return _json(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            _error_body(code=_GENERIC_500_CODE, message=_GENERIC_500_MESSAGE, run_id=run_id),
        )
    return _json(
        status.HTTP_502_BAD_GATEWAY,
        _error_body(
            code="restore_stage_failed",
            message=_scrub_text(exc.reason)[:_REASON_MAX_LEN],
            run_id=run_id,
            failing_stage=exc.stage,
        ),
    )


async def _handle_export_stage_failed_error(request: Request, exc: Exception) -> JSONResponse:
    """Shape an ``ExportStageFailedError`` body: HTTP 502, naming the failing stage.

    The message is the scrubbed stage reason cut to 300 characters. If ``exc`` is not an
    ``ExportStageFailedError`` (not expected: registered for that type only) a generic 500 is
    returned.
    """
    run_id = _bound_run_id(request)
    if not isinstance(exc, ExportStageFailedError):  # defensive — registered for this type only
        return _json(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            _error_body(code=_GENERIC_500_CODE, message=_GENERIC_500_MESSAGE, run_id=run_id),
        )
    return _json(
        status.HTTP_502_BAD_GATEWAY,
        _error_body(
            code="export_stage_failed",
            message=_scrub_text(exc.reason)[:_REASON_MAX_LEN],
            run_id=run_id,
            failing_stage=exc.stage,
        ),
    )


async def _handle_request_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Shape a ``RequestValidationError`` body: HTTP 422, generic message, no field detail."""
    del exc  # the raw errors can echo submitted values / paths — never surfaced
    return _json(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        _error_body(
            code=_VALIDATION_CODE, message=_VALIDATION_MESSAGE, run_id=_bound_run_id(request)
        ),
    )


async def _handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all: HTTP 500, generic message, no detail in the body.

    The cause is logged here, not upstream: one ``failed`` entry (component ``api``, action
    ``unhandled_request_error``) with the request's ``run_id`` (null when none is bound), the
    ``exception_type`` and a scrubbed, 300-character ``detail``. ``main.configure`` mirrors
    failed entries to stderr. A logging failure never changes the response; it leaves one
    stdlib record naming only the logging error's type.
    """
    run_id = _bound_run_id(request)
    try:
        # Deferred: stage_failure_log imports this module (cycle); also keeps this module free
        # of transitive imports (module docstring).
        from ps_service.api.stage_failure_log import stage_failure_detail  # noqa: PLC0415

        emit_log_entry(
            component="api",
            action="unhandled_request_error",
            outcome="failed",
            run_id=run_id,
            extra={"exception_type": type(exc).__name__, "detail": stage_failure_detail(exc)},
        )
    except Exception as emit_exc:  # noqa: BLE001 - logging must never mask the generic 500
        _LOG.error(
            "unhandled_request_error log entry could not be emitted (%s)",
            type(emit_exc).__name__,
        )
    return _json(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        _error_body(code=_GENERIC_500_CODE, message=_GENERIC_500_MESSAGE, run_id=run_id),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register every ``ps_service.api`` exception handler on ``app``.

    Called once from ``ps_service.main.create_app`` so that every error leaves the API in the
    single error-body shape with a scrubbed message.

    Status map (error type, HTTP status):

    * 400: ``InvalidAccessRoleError``, ``InvalidAuditQueryFilterError``.
    * 401: ``MergeApprovalRequiresAuthenticatedCallerError``; ``InternalIngestOwnerRequiredError``
      when the orchestration is given no owner for a Policy-bearing seed.
    * 403: ``AccessDeniedError``, ``SelfGrantOrRevokeBlockedError``,
      ``SystemOwnerFloorViolationError``.
    * 404: ``CatalogIdentifierNotFoundError``, ``PendingReviewNotFoundError``,
      ``ExportInstrumentNotFoundError``, ``PendingApprovalNotFoundError``,
      ``PendingApprovalInvalidOrExpiredError``, ``RestoreInstrumentIdNotFoundError``.
    * 409: ``ShortNameCollisionError``, ``CelexAlreadyIngestedError``,
      ``RestoreInstrumentIdAmbiguousError``.
    * 413: ``RequestBodyTooLargeError``.
    * 422: ``InternalSeedValidationError``, ``RestoreArtifactRejectedError``,
      ``RequestValidationError`` (generic message, no field detail).
    * 502: ``PipelineStageError``, ``RestoreStageFailedError``, ``ExportStageFailedError``
      (each naming the failing stage), ``CuratedSourceUnavailableError``.
    * 503: ``AuditTrailUnavailableError`` (the opening audit row could not be written),
      ``AuthorizationStoreUnavailableError``, ``CatalogSourceOverrideUnavailableError``,
      ``IngestionConfigIncompleteError``, ``GraphWriteUnavailableError`` (no graph write
      gateway, so the write fails closed), ``ExportConfigIncompleteError``.
    * 500: every other exception, with a generic message.

    Args:
        app: The FastAPI application to register handlers on.

    Returns:
        None.

    Side Effects:
        Adds the exception handlers to ``app``, replacing any handler already registered for the
        same exception type.
    """
    for exc_type, code, status_code in _API_ERROR_SPECS:
        app.add_exception_handler(exc_type, _make_verbatim_handler(code, status_code))
    app.add_exception_handler(PipelineStageError, _handle_pipeline_stage_error)
    app.add_exception_handler(RestoreStageFailedError, _handle_restore_stage_failed_error)
    app.add_exception_handler(ExportStageFailedError, _handle_export_stage_failed_error)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_exception)
