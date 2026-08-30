"""Pydantic request/response payload models for the PS Service REST API.

Per L2 Data Modeling every REST request/response body is a Pydantic model with
``Field()`` constraints on any value that reaches query construction, a
filesystem path, or an LLM call. This module holds the ``GET /regulations``
response shapes and the ``POST /ingestions`` request/response models (a
``source``-discriminated union of a catalog request and an internal-document
request, the accepted-response body, and the structured error body).
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


class CatalogIngestionRequest(BaseModel):
    """``POST /ingestions`` body naming a curated EU regulation by its CELEX identifier."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["catalog"]
    celex: str = Field(min_length=10, max_length=10, pattern=r"^3\d{4}[A-Z]\d{4}$")


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
