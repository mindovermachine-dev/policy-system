"""FastAPI exception handlers for the PS Service REST API.

Every 4xx/5xx body this API returns is minted here, in one shape
(``{"error": {"code", "message", "failing_stage"}, "run_id": ...}`` — AC-BI-009,
AC-BI-010). Messages from a whitelisted set of domain-level exception types are
surfaced verbatim after path/host scrubbing; every other exception collapses to a
generic message, with the real detail left for server-side logging only. No
stack trace, filesystem path, repo/home directory, ``host:port`` token, or URL
ever reaches a response body.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ps_service.api.errors import (
    ApiError,
    CatalogIdentifierNotFoundError,
    FixturePathError,
    IngestionConfigIncompleteError,
    InternalIngestionNotImplementedError,
    InternalSeedValidationError,
    PipelineStageError,
    RequestBodyTooLargeError,
    RestoreArtifactRejectedError,
    RestoreStageFailedError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import FastAPI, Request

# --- text scrubbing (AC-BI-009 / PLAN_REVIEWED.md §1.1 M5) --------------------

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

    Applied to every message surfaced to a caller. Regexes are bounded (a single
    character-class quantifier each — no nested quantifiers, no catastrophic
    backtracking).

    Args:
        text: The raw message.

    Returns:
        The message with each leak-shaped span replaced by a fixed placeholder.
    """
    scrubbed = _REPO_ROOT_RE.sub(_PATH_PLACEHOLDER, text)
    scrubbed = _HOME_DIR_RE.sub(_PATH_PLACEHOLDER, scrubbed)
    scrubbed = _URL_RE.sub(_URL_PLACEHOLDER, scrubbed)
    scrubbed = _HOST_PORT_RE.sub(_ADDR_PLACEHOLDER, scrubbed)
    scrubbed = _ABS_PATH_RE.sub(_PATH_PLACEHOLDER, scrubbed)
    scrubbed = _HOME_TILDE_RE.sub(_PATH_PLACEHOLDER, scrubbed)
    return _ENV_HOME_RE.sub(_PATH_PLACEHOLDER, scrubbed)


# --- safe-verbatim whitelist (PLAN_REVIEWED.md §1.1 M5) ----------------------

_SAFE_VERBATIM: tuple[type[ApiError], ...] = (
    CatalogIdentifierNotFoundError,
    FixturePathError,
    InternalSeedValidationError,
    InternalIngestionNotImplementedError,
    IngestionConfigIncompleteError,
    RequestBodyTooLargeError,
    RestoreArtifactRejectedError,
)
"""API-boundary error types whose ``str(exc)`` is domain-level and safe to surface."""

_SAFE_VERBATIM_NAMES: frozenset[str] = frozenset(
    {
        "CatalogIdentifierNotFoundError",
        "FixturePathError",
        "InternalSeedValidationError",
        "InternalIngestionNotImplementedError",
        "IngestionConfigIncompleteError",
        "DomainMapperExtractionError",
        "DomainMapperDerivationError",
        "DomainMapperGovernanceError",
        "CompanyMergeConfigurationError",
    }
)
"""Whitelisted domain-error class names, matched by name so this module never
imports ``ps_service.domain_mapper`` / ``ps_service.company_merge`` at load time
(layering + the Process Harness's no-transitive-pipeline-import guarantee)."""


def is_safe_verbatim(exc: BaseException) -> bool:
    """Return whether ``str(exc)`` may be surfaced to the caller (after scrubbing).

    True for the API-boundary types in :data:`_SAFE_VERBATIM` and, matched by
    class name, the whitelisted Domain Mapper / Company Merge domain errors.
    Everything else is treated as potentially detail-leaking. Used both here and
    by the orchestration's ``_run_stage`` when building a ``PipelineStageError``
    reason.

    Args:
        exc: The exception to classify.

    Returns:
        ``True`` if the exception's message is domain-level and caller-safe.
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
        failing_stage: The failing pipeline stage, for ``PipelineStageError`` only.

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

_API_ERROR_SPECS: tuple[tuple[type[ApiError], str, int], ...] = (
    (CatalogIdentifierNotFoundError, "catalog_identifier_not_found", status.HTTP_404_NOT_FOUND),
    (FixturePathError, "fixture_path_invalid", status.HTTP_400_BAD_REQUEST),
    (InternalSeedValidationError, "internal_seed_invalid", status.HTTP_422_UNPROCESSABLE_CONTENT),
    (
        InternalIngestionNotImplementedError,
        "internal_ingestion_not_implemented",
        status.HTTP_501_NOT_IMPLEMENTED,
    ),
    (
        IngestionConfigIncompleteError,
        "ingestion_config_incomplete",
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
)


def _make_verbatim_handler(
    code: str, status_code: int
) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
    """Build a handler that surfaces a whitelisted error's scrubbed message."""

    async def _handler(request: Request, exc: Exception) -> JSONResponse:
        message = _scrub_text(str(exc)) if is_safe_verbatim(exc) else _GENERIC_500_MESSAGE
        return _json(
            status_code,
            _error_body(code=code, message=message, run_id=_bound_run_id(request)),
        )

    return _handler


async def _handle_pipeline_stage_error(request: Request, exc: Exception) -> JSONResponse:
    """Shape a ``PipelineStageError`` body: HTTP 502, naming the failing stage (AC-BI-008)."""
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
    """Shape a ``RestoreStageFailedError`` body: HTTP 502, naming the failing stage (AC-BI-008)."""
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
    """Catch-all: HTTP 500, generic message, no detail (AC-BI-009). Detail is logged upstream."""
    del exc
    return _json(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        _error_body(
            code=_GENERIC_500_CODE, message=_GENERIC_500_MESSAGE, run_id=_bound_run_id(request)
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register every ``ps_service.api`` exception handler on ``app``.

    Called from ``ps_service.main.create_app``. Registers one handler per
    whitelisted ``ApiError`` subclass, one for ``PipelineStageError`` (502), one
    for ``RestoreStageFailedError`` (502), one for ``RequestValidationError``
    (422), and a catch-all ``Exception`` handler (generic 500). Status map:
    ``CatalogIdentifierNotFoundError`` 404, ``FixturePathError`` 400,
    ``InternalSeedValidationError`` 422,
    ``InternalIngestionNotImplementedError`` 501,
    ``IngestionConfigIncompleteError`` 503,
    ``RestoreArtifactRejectedError`` 422, ``RequestBodyTooLargeError`` 413,
    ``PipelineStageError`` 502, ``RestoreStageFailedError`` 502,
    ``RequestValidationError`` 422, everything else 500.

    Args:
        app: The FastAPI application to register handlers on.
    """
    for exc_type, code, status_code in _API_ERROR_SPECS:
        app.add_exception_handler(exc_type, _make_verbatim_handler(code, status_code))
    app.add_exception_handler(PipelineStageError, _handle_pipeline_stage_error)
    app.add_exception_handler(RestoreStageFailedError, _handle_restore_stage_failed_error)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_exception)
