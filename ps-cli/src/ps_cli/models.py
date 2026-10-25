"""Parsed-response shapes for PS Service's REST API.

Vendored per L2 Project Structure's "fully decoupled... vendors its own copy" rule.
These are `ps-cli`'s own shapes for the JSON bodies PS Service returns, not imports of
`ps_service.api.models` (that would violate the architecture boundary enforced by
`ps-cli/tests/test_architecture_boundary.py`). There is no Pydantic: a `TypedDict`
describes the raw JSON shape, and a frozen `dataclass` holds the parsed result a
command handler consumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import NotRequired, TypedDict


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


class StatusDetail(Enum):
    """How much of `GET /status` a caller asks for, and whether it may send a login.

    `NONE` never sends a bearer token, so it never reads the stored login. `IF_LOGGED_IN`
    sends one when a token can be obtained and otherwise accepts the status-only reply.
    `REQUIRED` fails closed without a login, and treats a reply without dependency
    names as an error.
    """

    NONE = "none"
    IF_LOGGED_IN = "if_logged_in"
    REQUIRED = "required"


class StatusResponseBody(TypedDict):
    """Raw JSON shape of a `GET /status` 200 response body.

    The two detail keys are present only for a caller whose bearer token PS Service
    verified.
    """

    status: str
    version: str
    unhealthy_dependencies: NotRequired[list[str]]
    gated_graphs: NotRequired[list[str]]


@dataclass(frozen=True)
class ServiceStatus:
    """Parsed result of `PsServiceClient.get_status()`.

    `unhealthy_dependencies` and `gated_graphs` are `None` when the reply carried no
    detail (the status-only reply to an anonymous caller), which is different from an
    empty list (the service reported that nothing is unhealthy or gated).
    """

    status: str
    version: str
    unhealthy_dependencies: list[str] | None
    gated_graphs: list[str] | None
