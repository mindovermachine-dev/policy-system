"""Parsed-response shapes for PS Service's REST API.

Vendored per L2 Project Structure's "fully decoupled... vendors its own copy" rule.
These are `ps-cli`'s own shapes for the JSON bodies PS Service returns, not imports of
`ps_service.api.models` (that would violate AC-BI-004's architecture boundary — see
`ps-cli/tests/test_architecture_boundary.py`). Per PLAN.md §1 D6, no Pydantic: a
`TypedDict` for the raw JSON shape, and a frozen `dataclass` for the parsed result a
command handler consumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


class StageOutcomeBody(TypedDict):
    """Raw JSON shape of one entry in a `POST /ingestions` 200 response's `stages` array."""

    stage: str
    status: str
    summary: dict[str, int]


class IngestionAcceptedResponseBody(TypedDict):
    """Raw JSON shape of a `POST /ingestions` 200 response body."""

    run_id: str
    regulatory_instrument_id: str
    source: str
    stages: list[StageOutcomeBody]


class ErrorDetailBody(TypedDict):
    """Raw JSON shape of the `error` object inside a structured `POST /ingestions` error body."""

    code: str
    message: str
    failing_stage: str | None


class ErrorResponseBody(TypedDict):
    """Raw JSON shape of a `POST /ingestions` non-2xx response body."""

    error: ErrorDetailBody
    run_id: str | None


@dataclass(frozen=True)
class StageOutcome:
    """One completed pipeline stage, as reported in an ingestion's success result."""

    stage: str
    status: str
    summary: dict[str, int]


@dataclass(frozen=True)
class IngestionResult:
    """Parsed success result of `PsServiceClient.ingest_internal()`."""

    run_id: str
    regulatory_instrument_id: str
    source: str
    stages: list[StageOutcome]


class RestorationStageOutcomeBody(TypedDict):
    """Raw JSON shape of one entry in a `POST /restorations` 200 response's `stages` array."""

    stage: str
    status: str


class RestorationAcceptedResponseBody(TypedDict):
    """Raw JSON shape of a `POST /restorations` 200 response body."""

    instrument_id: str
    stages: list[RestorationStageOutcomeBody]


@dataclass(frozen=True)
class RestorationStageOutcome:
    """One completed restore stage, as reported in a restore's success result."""

    stage: str
    status: str


@dataclass(frozen=True)
class RestorationResult:
    """Parsed success result of `PsServiceClient.restore_instrument()`."""

    instrument_id: str
    stages: list[RestorationStageOutcome]


class ExportManifestBody(TypedDict):
    """Raw JSON shape of the `manifest` field in a `POST /exports` 200 response body."""

    instrument_id: str
    celex: str | None
    title: str
    short_name: str
    version: str
    source_type: str
    jurisdiction: str | None
    schema_version: str
    exported_at: str
    baseline_sha256: str
    native_sha256: str


class ExportStageOutcomeBody(TypedDict):
    """Raw JSON shape of one entry in a `POST /exports` 200 response's `stages` array."""

    stage: str
    status: str


class ExportAcceptedResponseBody(TypedDict):
    """Raw JSON shape of a `POST /exports` 200 response body."""

    instrument_id: str
    manifest: ExportManifestBody
    baseline_blob_base64: str
    native_blob_base64: str
    stages: list[ExportStageOutcomeBody]


@dataclass(frozen=True)
class ExportManifest:
    """The `manifest.json` fields returned by `PsServiceClient.export_instrument()`."""

    instrument_id: str
    celex: str | None
    title: str
    short_name: str
    version: str
    source_type: str
    jurisdiction: str | None
    schema_version: str
    exported_at: str
    baseline_sha256: str
    native_sha256: str


@dataclass(frozen=True)
class ExportStageOutcome:
    """One completed export stage, as reported in an export's success result."""

    stage: str
    status: str


@dataclass(frozen=True)
class ExportResult:
    """Parsed success result of `PsServiceClient.export_instrument()`."""

    instrument_id: str
    manifest: ExportManifest
    baseline_blob_base64: str
    native_blob_base64: str
    stages: list[ExportStageOutcome]


class HealthResponseBody(TypedDict):
    """Raw JSON shape of a `GET /health` 200 response body."""

    status: str


class ReadyResponseBody(TypedDict):
    """Raw JSON shape of a `GET /ready` 200 response body."""

    status: str
    unhealthy_dependencies: list[str]


@dataclass(frozen=True)
class ReadinessResult:
    """Parsed result of `PsServiceClient.check_readiness()`."""

    status: str
    unhealthy_dependencies: list[str]
