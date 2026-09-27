"""Domain-specific exception types raised at the ``ps_service.api`` HTTP boundary.

Mirrors the shape of the other components' ``errors.py`` modules (one type per
distinct failure boundary, never a bare ``Exception``/``ValueError`` — L1/L2
Error Handling). Routes raise one of these; ``error_handlers.register_exception_handlers``
maps each to its HTTP status and mints the structured body (AC-BI-006, AC-BI-008,
AC-BI-009). ``fastapi.HTTPException`` with free text is never raised by this layer.
"""

from __future__ import annotations


class ApiError(Exception):
    """Base type for every ``ps_service.api`` boundary error. Never raised directly.

    Exists so ``error_handlers`` can whitelist the whole family and so callers
    can ``except ApiError`` without enumerating subclasses.
    """


class CatalogIdentifierNotFoundError(ApiError):
    """A ``POST /ingestions`` catalog request named a CELEX absent from the curated catalog.

    Raised by the route before any pipeline stage runs (AC-BI-006). Handled as
    HTTP 404; ``str(exc)`` is domain-level and surfaced verbatim.
    """


class InternalSeedValidationError(ApiError):
    """An internal seed document is malformed, carries an unknown label, or is not internal.

    The API-boundary translation of the internal-seed adapter's own
    ``InternalSeedError`` (layering — the adapter must not import
    ``ps_service.api``). Raised before any pipeline stage runs (AC-BI-006).
    Handled as HTTP 422; ``str(exc)`` is surfaced verbatim.
    """


class IngestionConfigIncompleteError(ApiError):
    """The resolved ``ServiceConfig`` is missing a value the pipeline needs.

    One of ``llm_interface_model`` / ``llm_interface_embed_model`` /
    ``company_merge_similarity_threshold`` is ``None``. Raised by the
    orchestration's config guard before any graph or stage call. Handled as
    HTTP 503; ``str(exc)`` is surfaced verbatim.
    """


class RestoreArtifactRejectedError(ApiError):
    """A ``POST /restorations`` artifact failed integrity/schema-version verification.

    The API-boundary translation of ``ps_service.restore.errors.
    ArtifactIntegrityError`` (a checksum mismatch, D9) or ``ps_service.restore.
    errors.ArtifactSchemaVersionMismatchError`` (D10) -- both raised by
    ``ps_service.restore.restore_instrument`` before any FalkorDB call, so a
    rejected artifact never causes even a staged-key write. Raised by
    ``api.restore_orchestration.run_restoration`` before any pipeline stage
    runs. Handled as HTTP 422; ``str(exc)`` is domain-level and surfaced
    verbatim.
    """


class RestoreStageFailedError(ApiError):
    """A restore stage raised; the restore did not complete (AC-BI-008).

    Carries the failing stage name and an already-sanitised reason, mirroring
    ``PipelineStageError``'s exact shape (one exception type per distinct
    failure boundary, L2 Error Handling). Handled as HTTP 502.
    """

    def __init__(self, *, stage: str, reason: str) -> None:
        """Record the failing stage and its sanitised reason.

        Args:
            stage: The restore stage that raised (e.g. ``"staging"``).
            reason: A caller-safe, already path/host-scrubbed reason string.
        """
        super().__init__(f"{stage} stage failed: {reason}")
        self.stage: str = stage
        self.reason: str = reason


class RequestBodyTooLargeError(ApiError):
    """A request body's ``Content-Length`` exceeds ``ServiceConfig.max_request_body_bytes``.

    Raised by ``main._MaxBodySizeMiddleware`` (a pure ASGI middleware, added
    directly on ``app`` -- not ``BaseHTTPMiddleware``, which buffers the
    whole body) before Starlette reads any request body bytes (CHANGES.md
    OQ7). Handled as HTTP 413; ``str(exc)`` is domain-level and surfaced
    verbatim.
    """


class PendingReviewNotFoundError(ApiError):
    """A `POST /near-misses/{review_id}/resolve` review id doesn't exist or was already resolved.

    A `PendingReview` node no longer existing IS "already resolved" in this
    design (issue #35, PLAN.md §2.1): a resolved review's node is deleted
    outright, never soft-status-changed, so a genuinely nonexistent id and
    an already-resolved one collapse to the same not-found condition
    (AC-BI-008). Raised by `api.near_miss_review_orchestration.
    run_resolve_near_miss` as the API-boundary translation of `ps_service.
    company_merge.pending_review.resolve_review`'s `None` return --
    mirrors `RestoreArtifactRejectedError`'s own "API-boundary translation
    of a lower-layer condition" pattern; `ps_service.company_merge` never
    raises or imports this type (M6 layering stays one-directional: `api`
    imports `company_merge` function-locally, never the reverse). Handled
    as HTTP 404; `str(exc)` is domain-level and surfaced verbatim.
    """


class MergeApprovalRequiresAuthenticatedCallerError(ApiError):
    """A `POST /near-misses/{review_id}/resolve` `decision="merge"` call has no real caller.

    Issue #131, CHANGES.md F1/AC-BI-002: `decision="merge"` now creates a
    signed-passkey pending approval rather than executing the merge
    synchronously, and that approval must be bound to a real, verified
    actor identity -- an unauthenticated request or one running under the
    local-test bypass (issue #67, which never establishes a real `Principal`)
    is refused before any Postgres or FalkorDB write, mirroring the MCP
    tool's own `_resolve_signing_actor()` fail-closed check
    (`mcp_interface.mcp_server`). Handled as HTTP 401, the same shape
    `RestAuthMiddleware`'s own 401 already uses; `str(exc)` is a fixed,
    domain-level message and safe to surface verbatim.
    """


class PendingApprovalNotFoundError(ApiError):
    """A `GET /near-misses/approvals/{pending_approval_id}` id is unknown or not this caller's.

    Issue #131, PLAN.md §2.3: deliberately raised for both "no such id" and
    "this id belongs to a different actor" -- the two are never
    distinguished in the response (AC-BI-015's leak-nothing rule, applied to
    this read side too), mirroring `ps_service.passkey_signing.service.
    check_pending_approval`'s own `None`-for-either-case return. Handled as
    HTTP 404; `str(exc)` is domain-level and safe to surface verbatim.
    """


class PendingApprovalInvalidOrExpiredError(ApiError):
    """A `/approvals/{id}/*` companion-browser call's code or state failed verification.

    Issue #131, CHANGES.md F2/F3: raised for an unknown `pending_approval_id`,
    a `code` whose `sha256` digest doesn't match the row's `code_hash`, and a
    row that is no longer `'pending'` or has passed its `expires_at` --
    deliberately never distinguished from one another in the response
    (AC-BI-015's leak-nothing rule: a caller must never learn *which* of
    these conditions applies). Raised by `ps_service.passkey_signing.router`'s
    handlers (looking up/verifying the code) and by
    `ps_service.passkey_signing.service._require_pending_and_unexpired`
    (F3's shared guard) before any WebAuthn library call or Postgres write.
    A fixed message baked into `__init__` (never a caller-supplied string) is
    what guarantees every raise site produces byte-identical text. Handled as
    HTTP 404; `str(exc)` is safe to surface verbatim.
    """

    def __init__(self) -> None:
        """Fix the message so every raise site is byte-identical (AC-BI-015)."""
        super().__init__("This approval link is no longer valid.")


class ExportInstrumentNotFoundError(ApiError):
    """A ``POST /exports`` ``instrument_id`` names no actually-ingested instrument.

    Raised by ``api.export_orchestration.run_export`` before any file I/O --
    either ``instrument_id`` is malformed (no ``{short}-{version}`` shape,
    zero graph access) or the derived ``{short}_baseline`` graph key doesn't
    exist yet (issue #71 CHANGES.md Appendix A1's safe ``db.list_graphs()``
    pre-check, before any ``MATCH`` is ever issued against that key) or the
    graph exists but carries no ``RegulatoryInstrument`` node under this id
    (PLAN.md D2, AC-BI-007). Handled as HTTP 404; ``str(exc)`` is domain-level
    and surfaced verbatim.
    """


class ExportConfigIncompleteError(ApiError):
    """The resolved ``ServiceConfig`` is missing the embedding model export needs.

    ``llm_interface_embed_model`` is ``None`` -- raised by the orchestration's
    config guard before any FalkorDB call (PLAN.md D3). Handled as HTTP 503;
    ``str(exc)`` is surfaced verbatim.
    """


class ExportStageFailedError(ApiError):
    """An export stage raised; the export did not complete (PLAN.md D3/AC-BI-011).

    Carries the failing stage name and an already-sanitised reason, mirroring
    ``RestoreStageFailedError``'s exact shape (one exception type per distinct
    failure boundary, L2 Error Handling). Handled as HTTP 502.
    """

    def __init__(self, *, stage: str, reason: str) -> None:
        """Record the failing stage and its sanitised reason.

        Args:
            stage: The export stage that raised (e.g. ``"serialization"``).
            reason: A caller-safe, already path/host-scrubbed reason string.
        """
        super().__init__(f"{stage} stage failed: {reason}")
        self.stage: str = stage
        self.reason: str = reason


class CuratedSourceUnavailableError(ApiError):
    """The configured curated-content source could not be fetched (issue #125, AC-BI-006).

    The API-boundary translation of ``ps_service.curated_source.errors.
    CuratedSourceFetchError`` -- raised by ``GET /catalog``'s route handler
    when the injected fetch dependency raises (the source is unreachable, or
    its response is missing/malformed). Never a silent fallback to stale
    data. Handled as HTTP 502; ``str(exc)`` is domain-level (names the source
    URL and the failure) and surfaced verbatim.
    """


class PipelineStageError(ApiError):
    """A pipeline stage raised; later stages were skipped (AC-BI-008).

    Carries the failing stage name and an already-sanitised reason so the
    handler can name the stage in the body without re-deriving it. Handled as
    HTTP 502.
    """

    def __init__(self, *, stage: str, reason: str) -> None:
        """Record the failing stage and its sanitised reason.

        Args:
            stage: The pipeline stage that raised (e.g. ``"extraction"``).
            reason: A caller-safe, already path/host-scrubbed reason string.
        """
        super().__init__(f"{stage} stage failed: {reason}")
        self.stage: str = stage
        self.reason: str = reason
