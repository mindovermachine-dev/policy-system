"""`PsServiceClient`: the sole channel `ps-cli` uses to reach PS Service, over REST.

Every network failure and every non-success response PS Service can return is
translated into `PsCliError` here (PLAN.md §1 D5) — command handlers and `cli.run()`
never see `httpx` exceptions directly.
"""

from __future__ import annotations

import base64
import sys
from typing import TYPE_CHECKING, Literal, NoReturn, Protocol, cast
from urllib.parse import urlparse

import httpx

from ps_cli.errors import PsCliError
from ps_cli.models import (
    ChangeCheckResult,
    ExportManifest,
    ExportResult,
    ExportStageOutcome,
    IngestionResult,
    InstrumentCheckOutcome,
    PendingReviewEntry,
    PendingReviewsResult,
    ReadinessResult,
    ResolveReviewResult,
    RestorationResult,
    RestorationStageOutcome,
    StageOutcome,
)

if TYPE_CHECKING:
    from ps_cli.catalog_repo import CuratedArtifact

_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})

_INSECURE_URL_WARNING = (
    "warning: PS Service URL '{url}' is not HTTPS and not a loopback address; "
    "requests will be sent in cleartext over the network."
)

_UNEXPECTED_RESPONSE_SHAPE_MSG = "PS Service returned an unexpected response shape"

_UNEXPECTED_ERROR_RESPONSE_MSG = (
    "PS Service returned an unexpected error response (status {status})"
)

_CONNECTION_ERROR_HINT = "check PS_CLI_SERVICE_URL / ps-cli.toml, and that ps-service is running"

_READ_TIMEOUT_MSG = "PS Service at {base_url} did not respond in time."

_INGESTIONS_PATH = "/ingestions"
_RESTORATIONS_PATH = "/restorations"
_EXPORTS_PATH = "/exports"
_HEALTH_PATH = "/health"
_READY_PATH = "/ready"
_CHANGE_CHECKS_PATH = "/change-checks"
_NEAR_MISSES_PATH = "/near-misses"

# `POST /ingestions` blocks synchronously for the entire real pipeline (Ingestion ->
# Domain Mapper -> Company Merge, no async job queue, by #51's own design) -- a real CRA
# ingestion measured 612.86s (10m12s) end to end. The client-wide timeout (30s read) is
# correct for a fast, static call like `GET /health` but far too short here, so this
# per-request override widens only the read timeout, only for `POST /ingestions`: 1800s
# (30 min) gives ~3x headroom over the observed real run for slower providers/larger
# regulations, while staying bounded (not infinite), per AC-BI-007's "actionable error,
# not a silent hang" intent for a genuinely stuck server. connect/write/pool stay at the
# fast client-wide 5s -- a slow *response* is expected for this endpoint, a slow
# *connection* is not. See OPEN_QUESTIONS_RESOLVED.md item 10 / PLAN.md's Increment 7
# AMENDMENT / briefs/BATCH_H_FIX.md.
_INGESTION_REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=1800.0, write=5.0, pool=5.0)

# `GET /ingestions/{run_id}` is a best-effort, fast poll of a run's currently-executing
# stage (AC-BI-008/009) -- unlike `POST /ingestions`, it never waits on the pipeline
# itself, so it stays at a short timeout, not `_INGESTION_REQUEST_TIMEOUT`'s 30 minutes.
_STATUS_POLL_TIMEOUT = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)

# `POST /restorations` never calls an LLM provider at all (D5/D6: restore's dedup replay
# reuses the artifact's own embeddings, no live RouteEmbedding call) -- unlike
# `_INGESTION_REQUEST_TIMEOUT`'s 30 minutes, there is no unbounded external-provider wait
# to accommodate here. Still wider than the fast client-wide 30s default: a large curated
# graph's staged writes + offline dedup merge run synchronously, in-process, on PS
# Service, so this is a documented, generous-but-bounded assumption (no real curated
# instrument has been timed yet), not a precisely measured value like ingestion's.
_RESTORATION_REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=5.0, pool=5.0)

# `POST /exports` always runs a real LLM embeddings backfill (PLAN.md §1 D8) -- unlike
# `_RESTORATION_REQUEST_TIMEOUT`'s carve-out above (restore's dedup replay reuses the
# artifact's own embeddings, no live RouteEmbedding call), export has no such shortcut.
# This mirrors `_INGESTION_REQUEST_TIMEOUT`'s own reasoning and exact value instead --
# a real, larger instrument's embedding backfill could plausibly exceed restore's
# shorter 300s budget.
_EXPORT_REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=1800.0, write=5.0, pool=5.0)

# `POST /change-checks` can call Cellar/ELI once per tracked instrument (poll) plus a
# full Ingestion-only re-ingest per finding -- potentially several sequential external
# calls (issue #73, PLAN.md §1 D15). Reuses `_INGESTION_REQUEST_TIMEOUT`'s own 1800s
# (30 min) read budget and rationale rather than inventing a new, unmeasured number --
# this plan's own reasonable, revisable choice, not a measured value (same category as
# `_RESTORATION_REQUEST_TIMEOUT`'s own flagged assumption above).
_CHANGE_CHECK_REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=1800.0, write=5.0, pool=5.0)


def _should_warn_insecure(url: str) -> bool:
    """Return whether `url` should trigger the AC-BI-009 insecure-connection warning.

    True iff the URL's scheme is not `https` AND its hostname is not one of the
    loopback spellings an operator would plausibly type (`127.0.0.1`, `localhost`,
    `::1`). Matches `ps_service/main.py::_is_loopback`'s heuristic in shape and in
    its documented limitation (exact-string match only, no CIDR-range matching) —
    a deliberately vendored copy, not shared code (PLAN.md §1 D4).
    """
    parsed = urlparse(url)
    return parsed.scheme != "https" and parsed.hostname not in _LOOPBACK_HOSTNAMES


def _raise_connection_error(base_url: str, cause: BaseException) -> NoReturn:
    """Raise the actionable `PsCliError` for a connect failure to `base_url` (D5)."""
    raise PsCliError(
        msg=f"Could not reach PS Service at {base_url}.",
        hint=_CONNECTION_ERROR_HINT,
    ) from cause


def _raise_read_timeout_error(base_url: str, cause: BaseException) -> NoReturn:
    """Raise the actionable `PsCliError` for a read-timeout waiting on `base_url` (D5)."""
    raise PsCliError(msg=_READ_TIMEOUT_MSG.format(base_url=base_url)) from cause


def _parse_health_body(payload: object) -> str:
    """Parse a `GET /health` 200 response body into its `status` string.

    Raises `PsCliError` (generic, defensive — D7) if the shape does not match.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    status = body.get("status")
    if not isinstance(status, str):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    return status


def _parse_readiness_body(payload: object) -> ReadinessResult:
    """Parse a `GET /ready` 200 response body into a `ReadinessResult`.

    Raises `PsCliError` (generic, defensive — D7) if the body does not match the
    expected `ReadyResponseBody` shape.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    status = body.get("status")
    unhealthy_raw = body.get("unhealthy_dependencies")
    if not isinstance(status, str) or not isinstance(unhealthy_raw, list):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    unhealthy_items = cast("list[object]", unhealthy_raw)
    if not all(isinstance(item, str) for item in unhealthy_items):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    return ReadinessResult(status=status, unhealthy_dependencies=cast("list[str]", unhealthy_items))


def _parse_service_version_body(payload: object) -> str:
    """Parse a `GET /health` 200 response body into its `version` string.

    Raises `PsCliError` (generic, defensive — D7) if the shape does not match.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    version = body.get("version")
    if not isinstance(version, str):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    return version


def _parse_stage_outcome(payload: object) -> StageOutcome:
    """Parse one raw JSON object into a `StageOutcome`.

    Raises `PsCliError` (generic, defensive — D5) if the shape does not match.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    stage = body.get("stage")
    status = body.get("status")
    summary_raw = body.get("summary")
    if (
        not isinstance(stage, str)
        or not isinstance(status, str)
        or not isinstance(summary_raw, dict)
    ):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    summary_items = cast("dict[str, object]", summary_raw)
    summary: dict[str, int] = {}
    for key, value in summary_items.items():
        # bool is a subclass of int; excluded explicitly so a stray boolean
        # summary value fails the shape check rather than silently coercing.
        if not isinstance(value, int) or isinstance(value, bool):
            raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
        summary[key] = value
    return StageOutcome(stage=stage, status=status, summary=summary)


def _parse_ingestion_response(payload: object) -> IngestionResult:
    """Parse a `POST /ingestions` 200 response body into an `IngestionResult`.

    Raises `PsCliError` (generic, defensive — D5) if the body does not match the
    expected `IngestionAcceptedResponse` shape. Shared verbatim by
    `ingest_catalog()` and `ingest_internal()` — both endpoints return the same
    success shape regardless of `source`.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    run_id = body.get("run_id")
    regulatory_instrument_id = body.get("regulatory_instrument_id")
    source = body.get("source")
    stages_raw = body.get("stages")
    if (
        not isinstance(run_id, str)
        or not isinstance(regulatory_instrument_id, str)
        or not isinstance(source, str)
        or not isinstance(stages_raw, list)
    ):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    stage_items = cast("list[object]", stages_raw)
    stages = [_parse_stage_outcome(item) for item in stage_items]
    return IngestionResult(
        run_id=run_id,
        regulatory_instrument_id=regulatory_instrument_id,
        source=source,
        stages=stages,
    )


def _parse_ingestion_status(payload: object) -> str | None:
    """Parse a `GET /ingestions/{run_id}` 200 response body into a stage name, or `None`.

    Unlike the other `_parse_*` helpers, this never raises `PsCliError` — a
    malformed or wrong-shaped body is treated the same as "no stage known",
    consistent with `poll_ingestion_status()`'s best-effort contract.
    """
    if not isinstance(payload, dict):
        return None
    body = cast("dict[str, object]", payload)
    stage = body.get("stage")
    return stage if isinstance(stage, str) else None


def _parse_restoration_stage_outcome(payload: object) -> RestorationStageOutcome:
    """Parse one raw JSON object into a `RestorationStageOutcome`.

    Raises `PsCliError` (generic, defensive — D5) if the shape does not match.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    stage = body.get("stage")
    status = body.get("status")
    if not isinstance(stage, str) or not isinstance(status, str):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    return RestorationStageOutcome(stage=stage, status=status)


def _parse_restoration_response(payload: object) -> RestorationResult:
    """Parse a `POST /restorations` 200 response body into a `RestorationResult`.

    Raises `PsCliError` (generic, defensive — D5) if the body does not match the
    expected `RestorationAcceptedResponse` shape.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    instrument_id = body.get("instrument_id")
    stages_raw = body.get("stages")
    if not isinstance(instrument_id, str) or not isinstance(stages_raw, list):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    stage_items = cast("list[object]", stages_raw)
    stages = [_parse_restoration_stage_outcome(item) for item in stage_items]
    return RestorationResult(instrument_id=instrument_id, stages=stages)


def _require_str_field(body: dict[str, object], key: str) -> str:
    """Return `body[key]` as `str`, or raise `PsCliError` if it is missing or not a string.

    A small shared helper for `_parse_export_manifest`'s nine required string
    fields -- extracted (L2 Common DRY: "extract... once a pattern repeats a
    third time") to keep that function's own cyclomatic complexity low rather
    than one large chained `isinstance` boolean expression.
    """
    value = body.get(key)
    if not isinstance(value, str):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    return value


def _optional_str_field(body: dict[str, object], key: str) -> str | None:
    """Return `body[key]` as `str | None`, or raise `PsCliError` if present but not a string."""
    value = body.get(key)
    if value is not None and not isinstance(value, str):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    return value


def _parse_export_manifest(payload: object) -> ExportManifest:
    """Parse the `manifest` field of a `POST /exports` 200 response body into an `ExportManifest`.

    Field-for-field mirror of `ExportManifestPayload` (`ps_service/api/models.py`) --
    vendored, never imported (AC-BI-004). Raises `PsCliError` (generic, defensive —
    D5) if the shape does not match.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    return ExportManifest(
        instrument_id=_require_str_field(body, "instrument_id"),
        celex=_optional_str_field(body, "celex"),
        title=_require_str_field(body, "title"),
        short_name=_require_str_field(body, "short_name"),
        version=_require_str_field(body, "version"),
        source_type=_require_str_field(body, "source_type"),
        jurisdiction=_optional_str_field(body, "jurisdiction"),
        schema_version=_require_str_field(body, "schema_version"),
        exported_at=_require_str_field(body, "exported_at"),
        baseline_sha256=_require_str_field(body, "baseline_sha256"),
        native_sha256=_require_str_field(body, "native_sha256"),
    )


def _parse_export_stage_outcome(payload: object) -> ExportStageOutcome:
    """Parse one raw JSON object into an `ExportStageOutcome`.

    Raises `PsCliError` (generic, defensive — D5) if the shape does not match.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    stage = body.get("stage")
    status = body.get("status")
    if not isinstance(stage, str) or not isinstance(status, str):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    return ExportStageOutcome(stage=stage, status=status)


def _parse_export_response(payload: object) -> ExportResult:
    """Parse a `POST /exports` 200 response body into an `ExportResult`.

    Raises `PsCliError` (generic, defensive — D5) if the body does not match the
    expected `ExportAcceptedResponseBody` shape.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    instrument_id = body.get("instrument_id")
    baseline_blob_base64 = body.get("baseline_blob_base64")
    native_blob_base64 = body.get("native_blob_base64")
    stages_raw = body.get("stages")
    if (
        not isinstance(instrument_id, str)
        or not isinstance(baseline_blob_base64, str)
        or not isinstance(native_blob_base64, str)
        or not isinstance(stages_raw, list)
    ):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    stage_items = cast("list[object]", stages_raw)
    stages = [_parse_export_stage_outcome(item) for item in stage_items]
    manifest = _parse_export_manifest(body.get("manifest"))
    return ExportResult(
        instrument_id=instrument_id,
        manifest=manifest,
        baseline_blob_base64=baseline_blob_base64,
        native_blob_base64=native_blob_base64,
        stages=stages,
    )


def _parse_instrument_check_outcome(payload: object) -> InstrumentCheckOutcome:
    """Parse one raw JSON object into an `InstrumentCheckOutcome`.

    Raises `PsCliError` (generic, defensive — D5) if the shape does not match.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    instrument_id = body.get("instrument_id")
    outcome = body.get("outcome")
    detail_raw = body.get("detail")
    reingest_run_id_raw = body.get("reingest_run_id")
    if not isinstance(instrument_id, str) or not isinstance(outcome, str):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    if detail_raw is not None and not isinstance(detail_raw, str):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    if reingest_run_id_raw is not None and not isinstance(reingest_run_id_raw, str):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    return InstrumentCheckOutcome(
        instrument_id=instrument_id,
        outcome=outcome,
        detail=detail_raw,
        reingest_run_id=reingest_run_id_raw,
    )


def _parse_change_check_response(payload: object) -> ChangeCheckResult:
    """Parse a `POST /change-checks` 200 response body into a `ChangeCheckResult`.

    Raises `PsCliError` (generic, defensive — D5) if the body does not match the
    expected `ChangeCheckResponse` shape.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    run_id = body.get("run_id")
    instruments_raw = body.get("instruments")
    if not isinstance(run_id, str) or not isinstance(instruments_raw, list):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    instrument_items = cast("list[object]", instruments_raw)
    instruments = [_parse_instrument_check_outcome(item) for item in instrument_items]
    return ChangeCheckResult(run_id=run_id, instruments=instruments)


def _parse_pending_review_entry(payload: object) -> PendingReviewEntry:
    """Parse one raw JSON object into a `PendingReviewEntry`.

    Raises `PsCliError` (generic, defensive — D5) if the shape does not match.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    review_id = body.get("id")
    kind = body.get("kind")
    incoming_text = body.get("incoming_text")
    nearest_existing_text = body.get("nearest_existing_text")
    similarity = body.get("similarity")
    if (
        not isinstance(review_id, str)
        or not isinstance(kind, str)
        or not isinstance(incoming_text, str)
        or not isinstance(nearest_existing_text, str)
        # bool is a subclass of int/float; excluded explicitly so a stray
        # boolean similarity value fails the shape check rather than
        # silently coercing (mirrors _parse_stage_outcome's own guard).
        or not isinstance(similarity, int | float)
        or isinstance(similarity, bool)
    ):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    return PendingReviewEntry(
        id=review_id,
        kind=kind,
        incoming_text=incoming_text,
        nearest_existing_text=nearest_existing_text,
        similarity=float(similarity),
    )


def _parse_pending_reviews_body(payload: object) -> PendingReviewsResult:
    """Parse a `GET /near-misses` 200 response body into a `PendingReviewsResult`.

    Raises `PsCliError` (generic, defensive — D5) if the body does not match the
    expected `PendingReviewListResponse` shape.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    reviews_raw = body.get("reviews")
    if not isinstance(reviews_raw, list):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    review_items = cast("list[object]", reviews_raw)
    reviews = [_parse_pending_review_entry(item) for item in review_items]
    return PendingReviewsResult(reviews=reviews)


def _parse_resolve_review_response(payload: object) -> ResolveReviewResult:
    """Parse a `POST /near-misses/{review_id}/resolve` 200 body into a `ResolveReviewResult`.

    Raises `PsCliError` (generic, defensive — D5) if the body does not match the
    expected `ResolveReviewResponse` shape.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    review_id = body.get("review_id")
    decision = body.get("decision")
    winner_id_raw = body.get("winner_id")
    loser_id_raw = body.get("loser_id")
    if not isinstance(review_id, str) or not isinstance(decision, str):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    if winner_id_raw is not None and not isinstance(winner_id_raw, str):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    if loser_id_raw is not None and not isinstance(loser_id_raw, str):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    return ResolveReviewResult(
        review_id=review_id,
        decision=decision,
        winner_id=winner_id_raw,
        loser_id=loser_id_raw,
    )


def _raise_from_error_body(response: httpx.Response) -> NoReturn:
    """Parse a non-2xx PS Service response into `PsCliError` per D5's mapping table.

    Expects the structured `ErrorBody` shape (`{"error": {"code", "message",
    "failing_stage"}, "run_id"}`); falls back to a generic `PsCliError` naming
    the HTTP status if the body is not JSON or does not match that shape —
    never assume the server always returns the documented shape. Shared
    verbatim by `ingest_catalog()`, `ingest_internal()`, and
    `restore_instrument()` — every PS Service endpoint returns this same
    structured error body shape.
    """
    generic_message = _UNEXPECTED_ERROR_RESPONSE_MSG.format(status=response.status_code)
    try:
        payload = response.json()
    except ValueError:
        raise PsCliError(msg=generic_message) from None

    if not isinstance(payload, dict):
        raise PsCliError(msg=generic_message)
    body = cast("dict[str, object]", payload)
    error_raw = body.get("error")
    run_id_raw = body.get("run_id")
    if not isinstance(error_raw, dict):
        raise PsCliError(msg=generic_message)
    error_body = cast("dict[str, object]", error_raw)
    code = error_body.get("code")
    message = error_body.get("message")
    failing_stage_raw = error_body.get("failing_stage")
    if not isinstance(code, str) or not isinstance(message, str):
        raise PsCliError(msg=generic_message)
    failing_stage = failing_stage_raw if isinstance(failing_stage_raw, str) else None
    run_id = run_id_raw if isinstance(run_id_raw, str) else None

    error_msg = f"PS Service reported {code}: {message}"
    if failing_stage:
        error_msg += f" (failing stage: {failing_stage})"
    hint = f"run_id: {run_id}" if run_id else None
    raise PsCliError(msg=error_msg, hint=hint)


class PsServiceClientProtocol(Protocol):
    """The structural shape command handlers and `cli.run()` depend on -- not `PsServiceClient`.

    L2 Common's Types Handling: "Use Protocol for interfaces" — matches the existing
    repo precedent for this exact shape (`ps_service/ingestion/adapters/base.py::IngestionAdapter`,
    `ps_service/api/ingestion_orchestration.py`'s stage Protocols). Command handlers
    (`ps_cli.modules.handlers`) and `ps_cli.cli.run()`'s `client` parameter are typed
    against this Protocol, not the concrete `PsServiceClient` class below, so a
    hand-written test fake satisfies the type structurally -- no `cast()` needed.
    """

    def check_health(self) -> str:
        """`GET /health`: whether the ASGI server is accepting connections."""
        ...

    def get_service_version(self) -> str:
        """`GET /health`: PS Service's installed package version."""
        ...

    def check_readiness(self) -> ReadinessResult:
        """`GET /ready`: readiness plus any currently-unhealthy dependency names."""
        ...

    def ingest_catalog(self, celex: str, *, run_id: str | None = None) -> IngestionResult:
        """`POST /ingestions` with `{"source": "catalog", "celex": celex}`."""
        ...

    def ingest_internal(self, content: dict[str, object]) -> IngestionResult:
        """`POST /ingestions` with `{"source": "internal", "content": content}`."""
        ...

    def poll_ingestion_status(self, run_id: str) -> str | None:
        """`GET /ingestions/{run_id}`: the run's currently-executing stage, best-effort."""
        ...

    def restore_instrument(self, artifact: CuratedArtifact) -> RestorationResult:
        """`POST /restorations` with `artifact`'s manifest fields + base64-encoded blobs."""
        ...

    def export_instrument(self, instrument_id: str) -> ExportResult:
        """`POST /exports` with `{"instrument_id": instrument_id}`."""
        ...

    def run_change_check(self) -> ChangeCheckResult:
        """`POST /change-checks`: sweep tracked instruments, re-ingesting any amendments found."""
        ...

    def list_pending_reviews(self) -> PendingReviewsResult:
        """`GET /near-misses`: every unresolved near-miss `PendingReview` (issue #35, AC-BI-003)."""
        ...

    def resolve_review(
        self, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveReviewResult:
        """`POST /near-misses/{review_id}/resolve` (issue #35, AC-BI-004/005/006/007/008/009)."""
        ...


class PsServiceClient:
    """Thin REST client over PS Service's `POST /ingestions` and related endpoints."""

    def __init__(self, base_url: str, *, transport: httpx.BaseTransport | None = None) -> None:
        """Construct the client, warning on stderr once if `base_url` looks insecure.

        `transport` is the constructor-injection seam tests use to substitute
        `httpx.MockTransport` for a real network connection (L2 Common: "no DI
        framework... take dependencies as constructor/function arguments").
        """
        if _should_warn_insecure(base_url):
            print(_INSECURE_URL_WARNING.format(url=base_url), file=sys.stderr)
        self._base_url = base_url
        self._client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0),
            transport=transport,
        )

    def check_health(self) -> str:
        """`GET /health`: whether the ASGI server is accepting connections.

        Raises `PsCliError` if PS Service cannot be reached or the connection
        is interrupted (refused, reset, or timed out) or if the response body
        does not match the expected shape. Never checks external dependencies
        (D6) — a healthy result here does not imply `check_readiness()` will
        also succeed.
        """
        try:
            response = self._client.get(_HEALTH_PATH)
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(self._base_url, exc)
        except httpx.TransportError as exc:
            _raise_connection_error(self._base_url, exc)
        return _parse_health_body(response.json())

    def get_service_version(self) -> str:
        """`GET /health`: PS Service's installed package version.

        Raises `PsCliError` if PS Service cannot be reached or the connection
        is interrupted (refused, reset, or timed out) or if the response body
        does not match the expected shape.
        """
        try:
            response = self._client.get(_HEALTH_PATH)
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(self._base_url, exc)
        except httpx.TransportError as exc:
            _raise_connection_error(self._base_url, exc)
        return _parse_service_version_body(response.json())

    def check_readiness(self) -> ReadinessResult:
        """`GET /ready`: readiness plus any currently-unhealthy dependency names.

        Reports whether PS Service's startup checks completed, and which
        dependencies (if any) are currently recorded unhealthy.

        Raises `PsCliError` if PS Service cannot be reached or the connection
        is interrupted (refused, reset, or timed out) or if the response body
        does not match the expected shape.
        """
        try:
            response = self._client.get(_READY_PATH)
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(self._base_url, exc)
        except httpx.TransportError as exc:
            _raise_connection_error(self._base_url, exc)
        return _parse_readiness_body(response.json())

    def ingest_catalog(self, celex: str, *, run_id: str | None = None) -> IngestionResult:
        """`POST /ingestions` with `{"source": "catalog", "celex": celex}`.

        Ingests a curated EU regulation, identified by its CELEX identifier,
        into the graph. When `run_id` is given, it is included in the request
        body so the caller can correlate this run with `poll_ingestion_status()`
        (AC-BI-009); when omitted (the default), the body is unchanged from
        today's exact wire shape. Raises `PsCliError` if PS Service cannot be
        reached, if it returns a non-2xx response (parsed per D5's error-body
        mapping — `failing_stage`, when present, is included in the raised
        message), or if a 200 response body does not match the expected
        success shape.
        """
        body: dict[str, str] = {"source": "catalog", "celex": celex}
        if run_id is not None:
            body["run_id"] = run_id
        try:
            response = self._client.post(
                _INGESTIONS_PATH,
                json=body,
                timeout=_INGESTION_REQUEST_TIMEOUT,
            )
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(self._base_url, exc)
        except httpx.TransportError as exc:
            _raise_connection_error(self._base_url, exc)
        if not response.is_success:
            _raise_from_error_body(response)
        return _parse_ingestion_response(response.json())

    def restore_instrument(self, artifact: CuratedArtifact) -> RestorationResult:
        """`POST /restorations` with `artifact`'s manifest fields + base64-encoded blobs.

        `artifact` was read locally off `curated_repo_path` by
        `ps_cli.catalog_repo.read_artifact()` — this method never touches the
        local filesystem itself, only uploads what it was given (D5: `ps-cli`
        reads the artifact locally, PS Service does the FalkorDB work).
        `baseline_blob`/`native_blob` are base64-encoded verbatim, unparsed —
        `ps-cli` never inspects their JSON content (CHANGES2.md §3.7). Raises
        `PsCliError` if PS Service cannot be reached, if it returns a non-2xx
        response (parsed per D5's error-body mapping — a checksum/
        schema_version rejection surfaces as `restore_artifact_rejected`, any
        other restore failure as `restore_stage_failed` naming the failing
        stage), or if a 200 response body does not match the expected
        success shape.
        """
        manifest = artifact.manifest
        body = {
            "instrument_id": manifest.instrument_id,
            "manifest": {
                "instrument_id": manifest.instrument_id,
                "celex": manifest.celex,
                "title": manifest.title,
                "short_name": manifest.short_name,
                "version": manifest.version,
                "source_type": manifest.source_type,
                "jurisdiction": manifest.jurisdiction,
                "schema_version": manifest.schema_version,
                "exported_at": manifest.exported_at,
                "baseline_sha256": manifest.baseline_sha256,
                "native_sha256": manifest.native_sha256,
            },
            "baseline_blob_base64": base64.b64encode(artifact.baseline_blob).decode("ascii"),
            "native_blob_base64": base64.b64encode(artifact.native_blob).decode("ascii"),
        }
        try:
            response = self._client.post(
                _RESTORATIONS_PATH,
                json=body,
                timeout=_RESTORATION_REQUEST_TIMEOUT,
            )
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(self._base_url, exc)
        except httpx.TransportError as exc:
            _raise_connection_error(self._base_url, exc)
        if not response.is_success:
            _raise_from_error_body(response)
        return _parse_restoration_response(response.json())

    def export_instrument(self, instrument_id: str) -> ExportResult:
        """`POST /exports` with `{"instrument_id": instrument_id}`.

        Exports an already-ingested instrument's baseline/native graphs plus
        a generated manifest -- PS Service derives every other descriptor
        field (`title`, `source_type`, `jurisdiction`, ...) server-side
        against the actually-ingested graph; `ps-cli` sends only the id
        (PLAN.md §1 D2/D11). Raises `PsCliError` if PS Service cannot be
        reached, if it returns a non-2xx response (parsed per D5/D7's
        error-body mapping -- an unknown instrument id surfaces as
        `export_instrument_not_found`, any pipeline failure as
        `export_stage_failed` naming the failing stage, an incomplete LLM
        Interface config as `export_config_incomplete`), or if a 200
        response body does not match the expected success shape. Uses
        `_EXPORT_REQUEST_TIMEOUT` (D8) -- export always runs a real LLM
        embeddings backfill, unlike `restore_instrument()`'s shorter one.
        """
        try:
            response = self._client.post(
                _EXPORTS_PATH,
                json={"instrument_id": instrument_id},
                timeout=_EXPORT_REQUEST_TIMEOUT,
            )
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(self._base_url, exc)
        except httpx.TransportError as exc:
            _raise_connection_error(self._base_url, exc)
        if not response.is_success:
            _raise_from_error_body(response)
        return _parse_export_response(response.json())

    def run_change_check(self) -> ChangeCheckResult:
        """`POST /change-checks`: sweep tracked instruments, re-ingesting any amendments found.

        No request body -- the sweep always covers the whole tracked catalog
        (issue #73, PLAN.md §1 D13). Raises `PsCliError` if PS Service cannot
        be reached or the connection is interrupted (refused, reset, or timed
        out), if it returns a
        non-2xx response (parsed per D5's error-body mapping -- `/change-checks`
        can still fail with the standard structured `ErrorBody` shape, e.g. a
        generic 500 from an unguarded graph-open failure, D12/D14), or if a 200
        response body does not match the expected success shape.
        """
        try:
            response = self._client.post(
                _CHANGE_CHECKS_PATH,
                timeout=_CHANGE_CHECK_REQUEST_TIMEOUT,
            )
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(self._base_url, exc)
        except httpx.TransportError as exc:
            _raise_connection_error(self._base_url, exc)
        if not response.is_success:
            _raise_from_error_body(response)
        return _parse_change_check_response(response.json())

    def poll_ingestion_status(self, run_id: str) -> str | None:
        """`GET /ingestions/{run_id}`: the run's currently-executing stage, best-effort.

        A live-progress read only (AC-BI-008/009), not authoritative resource
        retrieval — every failure (network error, non-2xx response, a
        non-JSON or wrong-shaped body) is swallowed and reported as `None`,
        never raised as `PsCliError` or any other exception, so a poll
        failure can never affect the caller's own `ingest_catalog()` result.
        """
        try:
            response = self._client.get(
                f"{_INGESTIONS_PATH}/{run_id}",
                timeout=_STATUS_POLL_TIMEOUT,
            )
        except httpx.HTTPError:
            return None
        if not response.is_success:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        return _parse_ingestion_status(payload)

    def ingest_internal(self, content: dict[str, object]) -> IngestionResult:
        """`POST /ingestions` with `{"source": "internal", "content": content}`.

        Ingests an internal document whose content was already read and
        parsed locally by `ps_cli.intake_validation.validate_local_seed_file`
        -- `content` is sent directly in the request body, nested as JSON,
        never as a filesystem path (issue #91). Raises `PsCliError` if PS
        Service cannot be reached, if it returns a non-2xx response (parsed
        per D5's error-body mapping -- today this always includes a 501
        `internal_ingestion_not_implemented` until issue #54's backend
        lands), or if a 200 response body does not match the expected
        success shape.
        """
        try:
            response = self._client.post(
                _INGESTIONS_PATH,
                json={"source": "internal", "content": content},
                timeout=_INGESTION_REQUEST_TIMEOUT,
            )
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(self._base_url, exc)
        except httpx.TransportError as exc:
            _raise_connection_error(self._base_url, exc)
        if not response.is_success:
            _raise_from_error_body(response)
        return _parse_ingestion_response(response.json())

    def list_pending_reviews(self) -> PendingReviewsResult:
        """`GET /near-misses`: every unresolved near-miss `PendingReview` (issue #35, AC-BI-003).

        Raises `PsCliError` if PS Service cannot be reached or the connection
        is interrupted (refused, reset, or timed out) or if the response body
        does not match the expected shape.
        """
        try:
            response = self._client.get(_NEAR_MISSES_PATH)
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(self._base_url, exc)
        except httpx.TransportError as exc:
            _raise_connection_error(self._base_url, exc)
        if not response.is_success:
            _raise_from_error_body(response)
        return _parse_pending_reviews_body(response.json())

    def resolve_review(
        self, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveReviewResult:
        """`POST /near-misses/{review_id}/resolve`: resolve one PendingReview (issue #35).

        `decision="keep-separate"` clears the pending review only
        (AC-BI-004). `decision="merge"` re-points every edge referencing the
        loser canonical node onto the deterministically-chosen winner,
        deletes the loser, and deletes the pending review, atomically
        (AC-BI-005/006/007). Raises `PsCliError` if PS Service cannot be
        reached or the connection is interrupted (refused, reset, or timed
        out), if it returns a
        non-2xx response (parsed per D5's error-body mapping -- a not-found,
        already-resolved, or (merge only) stale `review_id` surfaces as
        `pending_review_not_found`, AC-BI-008), or if a 200 response body
        does not match the expected success shape.
        """
        try:
            response = self._client.post(
                f"{_NEAR_MISSES_PATH}/{review_id}/resolve",
                json={"decision": decision},
            )
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(self._base_url, exc)
        except httpx.TransportError as exc:
            _raise_connection_error(self._base_url, exc)
        if not response.is_success:
            _raise_from_error_body(response)
        return _parse_resolve_review_response(response.json())
