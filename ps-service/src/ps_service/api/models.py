"""Pydantic request/response payload models for the PS Service REST API.

Per L2 Data Modeling every REST request/response body is a Pydantic model with
``Field()`` constraints on any value that reaches query construction, a
filesystem path, or an LLM call. This module holds the ``GET /regulations``
response shapes and the ``POST /ingestions`` request/response models (a
``source``-discriminated union of a catalog request and an internal-document
request, the accepted-response body, and the structured error body), plus the
``POST /restorations`` request/response models (D5, PLAN.md Batch 6).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RegulationCatalogEntry(BaseModel):
    """One regulation as returned by ``GET /regulations``."""

    model_config = ConfigDict(frozen=True)

    celex: str = Field(min_length=10, max_length=10)
    title: str = Field(min_length=1)


class RegulationCatalogResponse(BaseModel):
    """Response body for ``GET /regulations``: the curated catalog plus the request run id."""

    model_config = ConfigDict(frozen=True)

    regulations: list[RegulationCatalogEntry]
    run_id: str = Field(min_length=1)


class CatalogInstrumentEntry(BaseModel):
    """One curated instrument as returned by ``GET /catalog`` (AC-BI-011).

    Unlike :class:`RegulationCatalogEntry` (``GET /regulations``'s narrower,
    CELEX-only contract, D12), this carries every curated instrument --
    external and internal -- with no ``celex`` field at all: an internal
    source (D15) has none, and a client driving ``ps-cli catalog list``
    never needs it (the CELEX-specific fast path is ``POST /ingestions``'s
    concern, not this listing's).
    """

    model_config = ConfigDict(frozen=True)

    instrument_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source_type: Literal["external", "internal"]
    jurisdiction: str | None = None


class CuratedCatalogResponse(BaseModel):
    """Response body for ``GET /catalog``: every curated instrument, unfiltered."""

    model_config = ConfigDict(frozen=True)

    instruments: list[CatalogInstrumentEntry]


class CatalogIngestionRequest(BaseModel):
    """``POST /ingestions`` body naming a curated EU regulation by its CELEX identifier.

    ``run_id`` is an optional, client-supplied correlation id (safe as both a
    URL path segment and a dict key) letting a caller poll
    ``GET /ingestions/{run_id}`` for live progress *while* this request is
    still in flight (AC-BI-008). When omitted, the route falls back to a
    fresh server-minted id -- today's exact behaviour, unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["catalog"]
    celex: str = Field(min_length=10, max_length=10, pattern=r"^3\d{4}[A-Z]\d{4}$")
    run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class InternalIngestionRequest(BaseModel):
    """``POST /ingestions`` body naming an internal-document JSON fixture by relative path.

    ``fixture_path`` is constrained to a ``.json`` file reachable by a relative
    path of safe characters; the ``_no_traversal`` validator additionally rejects
    any ``..`` segment, a leading ``/``, or a backslash so the value cannot escape
    the fixtures root when it is later resolved (AC-BI-007, first of two layers).
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["internal"]
    fixture_path: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/\-]*\.json$",
    )

    @field_validator("fixture_path")
    @classmethod
    def _no_traversal(cls, v: str) -> str:
        """Reject a ``..`` path segment, a leading ``/``, or a backslash.

        Raising ``ValueError`` here (rather than the L2-preferred domain
        exception) is deliberate: Pydantic only converts ``ValueError`` /
        ``AssertionError`` from a field validator into a ``ValidationError``,
        which is what the API boundary must surface as a 422.

        Args:
            v: The candidate relative fixture path.

        Returns:
            The unchanged value when it contains no traversal construct.

        Raises:
            ValueError: If the value could escape the fixtures root (leading
                slash, a backslash separator, or a ``..`` segment).
        """
        if v.startswith("/") or "\\" in v:
            raise ValueError("fixture_path must be a relative POSIX path")
        if any(segment == ".." for segment in v.split("/")):
            raise ValueError("fixture_path must not contain a '..' segment")
        return v


IngestionRequest = Annotated[
    CatalogIngestionRequest | InternalIngestionRequest,
    Field(discriminator="source"),
]
"""The ``POST /ingestions`` request body: a ``source``-discriminated union."""


class StageOutcome(BaseModel):
    """One completed pipeline stage as reported in the accepted response."""

    model_config = ConfigDict(frozen=True)

    stage: str = Field(min_length=1)
    status: Literal["succeeded"]
    summary: dict[str, int]


class IngestionAcceptedResponse(BaseModel):
    """Success body for ``POST /ingestions``: the run id, instrument id, and per-stage outcomes."""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1)
    regulatory_instrument_id: str = Field(min_length=1)
    source: Literal["catalog", "internal"]
    stages: list[StageOutcome]


class IngestionStatusResponse(BaseModel):
    """Response body for ``GET /ingestions/{run_id}``: a best-effort live-progress read.

    Always returned with HTTP 200, including for an unknown, completed, or
    not-yet-started ``run_id`` (``stage: None``) -- this is a best-effort
    observation of ``ps_service.api.run_status``, not authoritative resource
    retrieval, so there is no 404 branch (AC-BI-008, D4).
    """

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1)
    stage: str | None = None


class RestorationManifestPayload(BaseModel):
    """The ``manifest.json`` fields carried inline in a ``POST /restorations`` body.

    Field-for-field mirror of ``ps_service.export.models.InstrumentManifest``
    (PLAN.md D1/D12) -- a plain dataclass, never itself a Pydantic model, since
    it is internal pipeline plumbing shared by Export and Restore. The REST
    boundary re-declares it here as a nested Pydantic model (L2 Data Modeling:
    every REST request body is Pydantic) and ``api.restore_orchestration``
    converts it to an ``InstrumentManifest`` before calling
    ``ps_service.restore.restore_instrument``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str = Field(min_length=1)
    celex: str | None = Field(default=None, min_length=10, max_length=10)
    title: str = Field(min_length=1)
    short_name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source_type: Literal["external", "internal"]
    jurisdiction: str | None = None
    schema_version: str = Field(min_length=1)
    exported_at: str = Field(min_length=1)
    baseline_sha256: str = Field(min_length=64, max_length=64)
    native_sha256: str = Field(min_length=64, max_length=64)


class RestorationRequest(BaseModel):
    """``POST /restorations`` body: one curated instrument's artifact (D5).

    ``baseline_blob_base64``/``native_blob_base64`` are the artifact's two
    UTF-8 JSON blobs (``export.serialize.to_json_bytes`` output, CHANGES2.md
    §2.1), base64-encoded for JSON transport -- ``ps-cli`` reads them off
    disk locally and uploads the bytes verbatim; PS Service never re-encodes
    what it decodes here (D5's client-driven-trigger, server-side-pipeline
    shape).
    """

    model_config = ConfigDict(extra="forbid")

    instrument_id: str = Field(min_length=1)
    manifest: RestorationManifestPayload
    baseline_blob_base64: str = Field(min_length=1)
    native_blob_base64: str = Field(min_length=1)


class RestorationStageOutcome(BaseModel):
    """One completed restore stage as reported in the accepted response."""

    model_config = ConfigDict(frozen=True)

    stage: str = Field(min_length=1)
    status: Literal["succeeded"]


class RestorationAcceptedResponse(BaseModel):
    """Success body for ``POST /restorations``: the instrument id and per-stage outcomes."""

    model_config = ConfigDict(frozen=True)

    instrument_id: str = Field(min_length=1)
    stages: list[RestorationStageOutcome]


class ErrorDetail(BaseModel):
    """The ``error`` object inside a structured error body."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    failing_stage: str | None = None


class ErrorBody(BaseModel):
    """Structured 4xx/5xx response body: a sanitised error plus the request run id (if bound)."""

    model_config = ConfigDict(frozen=True)

    error: ErrorDetail
    run_id: str | None
