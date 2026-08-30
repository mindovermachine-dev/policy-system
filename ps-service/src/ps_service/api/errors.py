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


class FixturePathError(ApiError):
    """An internal-document request's fixture path resolved outside the fixtures root.

    Also covers a non-``.json`` suffix or a missing file (AC-BI-007). Raised by
    ``fixtures.resolve_fixture_path`` at point of use, before any pipeline stage
    runs. Handled as HTTP 400; ``str(exc)`` is surfaced verbatim.
    """


class InternalSeedValidationError(ApiError):
    """An internal seed document is malformed, carries an unknown label, or is not internal.

    The API-boundary translation of the internal-seed adapter's own
    ``InternalSeedError`` (layering — the adapter must not import
    ``ps_service.api``). Raised before any pipeline stage runs (AC-BI-006).
    Handled as HTTP 422; ``str(exc)`` is surfaced verbatim.
    """


class InternalIngestionNotImplementedError(ApiError):
    """A ``POST /ingestions`` request selected ``source: "internal"``, which this release omits.

    Internal-document ingestion (the internal Ingestion + Domain Mapping adapter
    pair plus governance derivation) is split out to
    ``mindovermachine-dev/policy-system#54``. Until it lands, a well-formed
    internal request validates and then returns a clean HTTP 501; a malformed
    one still 422s at Pydantic validation. Handled as HTTP 501; ``str(exc)``
    names the tracking issue and is surfaced verbatim.
    """


class IngestionConfigIncompleteError(ApiError):
    """The resolved ``ServiceConfig`` is missing a value the pipeline needs.

    One of ``llm_interface_model`` / ``llm_interface_embed_model`` /
    ``company_merge_similarity_threshold`` is ``None``. Raised by the
    orchestration's config guard before any graph or stage call. Handled as
    HTTP 503; ``str(exc)`` is surfaced verbatim.
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
