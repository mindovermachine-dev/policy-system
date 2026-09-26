"""`PsServiceClient`: the sole channel `ps-cli` uses to reach PS Service, over REST.

Every network failure and every non-success response PS Service can return is
translated into `PsCliError` here (PLAN.md §1 D5) — command handlers and `cli.run()`
never see `httpx` exceptions directly.
"""

from __future__ import annotations

import sys
from http import HTTPStatus
from typing import TYPE_CHECKING, NoReturn, Protocol, cast
from urllib.parse import urlparse

import httpx

from ps_cli import device_flow
from ps_cli.errors import PsCliError
from ps_cli.models import (
    ExportManifest,
    ExportResult,
    ExportStageOutcome,
    IngestionResult,
    ReadinessResult,
    StageOutcome,
)

if TYPE_CHECKING:
    from ps_cli.credentials import CredentialStore
    from ps_cli.targets import AuthOverrides

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

# AC-BI-014's literal actionable wording -- checked before `_raise_from_error_body`
# (D-57 group 3, Slice 15/18) so a 401's own structured error body, if PS Service's
# error middleware ever attached one, never leaks into this message.
_AUTHENTICATION_REJECTED_MSG = "authentication rejected by {base_url}; run `ps-cli auth login`"

_READ_TIMEOUT_MSG = "PS Service at {base_url} did not respond in time."

_INGESTIONS_PATH = "/ingestions"
_EXPORTS_PATH = "/exports"
_HEALTH_PATH = "/health"
_READY_PATH = "/ready"

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

# `POST /exports` always runs a real LLM embeddings backfill (PLAN.md §1 D8) -- unlike
# a restore-from-catalog's dedup replay (which reuses the artifact's own embeddings, no
# live RouteEmbedding call; issue #127 moved that path off `ps-cli` entirely, onto the
# `ps-restore-instrument` skill), export has no such shortcut. This mirrors
# `_INGESTION_REQUEST_TIMEOUT`'s own reasoning and exact value instead -- a real, larger
# instrument's embedding backfill could plausibly exceed a shorter restore-style budget.
_EXPORT_REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=1800.0, write=5.0, pool=5.0)


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
    expected `IngestionAcceptedResponse` shape. Used by `ingest_internal()`.
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


def _raise_from_error_body(response: httpx.Response) -> NoReturn:
    """Parse a non-2xx PS Service response into `PsCliError` per D5's mapping table.

    Expects the structured `ErrorBody` shape (`{"error": {"code", "message",
    "failing_stage"}, "run_id"}`); falls back to a generic `PsCliError` naming
    the HTTP status if the body is not JSON or does not match that shape —
    never assume the server always returns the documented shape. Shared
    verbatim by `ingest_internal()` and `export_instrument()` — every PS
    Service endpoint returns this same structured error body shape.
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

    def ingest_internal(self, content: dict[str, object]) -> IngestionResult:
        """`POST /ingestions` with `{"source": "internal", "content": content}`."""
        ...

    def export_instrument(self, instrument_id: str) -> ExportResult:
        """`POST /exports` with `{"instrument_id": instrument_id}`."""
        ...


class PsServiceClient:
    """Thin REST client over PS Service's `POST /ingestions` and related endpoints."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        credential_store: CredentialStore | None = None,
        context: str | None = None,
        auth_override: AuthOverrides | None = None,
    ) -> None:
        """Construct the client, warning on stderr once if `base_url` looks insecure.

        `transport` is the constructor-injection seam tests use to substitute
        `httpx.MockTransport` for a real network connection (L2 Common: "no DI
        framework... take dependencies as constructor/function arguments").

        `credential_store`/`context`/`auth_override` (issue #57 Slice 15, AC-BI-011)
        are all optional and default to `None`, so every pre-#57 construction site
        (`PsServiceClient(base_url)`, `PsServiceClient(base_url, transport=...)`) is
        byte-for-byte unaffected. When `credential_store` and `context` are both
        given, every authenticated call (`_authenticated_post`) attaches a bearer
        token; when either is `None`, no header is ever attached
        and `check_health`/`get_service_version`/`check_readiness` never attach one
        regardless (D-57-6).
        """
        if _should_warn_insecure(base_url):
            print(_INSECURE_URL_WARNING.format(url=base_url), file=sys.stderr)
        self._base_url = base_url
        self._credential_store = credential_store
        self._context = context
        self._auth_override = auth_override
        # Issue #121: this invocation's in-memory-only access token cache -- one
        # `PsServiceClient` is built once per real CLI invocation, so its lifetime
        # already matches "one invocation" by construction (AC-BI-003/004).
        self._access_token_cache = device_flow.AccessTokenCache()
        self._transport = transport
        self._client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0),
            transport=transport,
        )

    def _authorization_headers(self) -> dict[str, str]:
        """`{"Authorization": "Bearer <token>"}` when credentials are configured, else `{}`.

        Refreshes (or fails closed) via `device_flow.ensure_valid_access_token` --
        AC-BI-012/013. Returns `{}` -- no network call, no store read -- whenever
        `credential_store` or `context` was not given at construction (D-57-6's
        unauthenticated-call shape, also relied on by `check_health`/
        `get_service_version`/`check_readiness` via `_get`/`_post`, which never
        call this method at all).
        """
        if self._credential_store is None or self._context is None:
            return {}
        access_token = device_flow.ensure_valid_access_token(
            context=self._context,
            service_url=self._base_url,
            auth_override=self._auth_override,
            credential_store=self._credential_store,
            access_token_cache=self._access_token_cache,
            transport=self._transport,
        )
        return {"Authorization": f"Bearer {access_token}"}

    def _raise_if_unauthorized(self, response: httpx.Response) -> None:
        """Raise AC-BI-014's actionable `PsCliError` on a 401, before any other check.

        Called by `_authenticated_post` immediately after the
        request returns, strictly before `_raise_from_error_body` -- so a 401's own
        structured error body (`code`/`message`), if PS Service's error middleware
        ever attached one, is never parsed or surfaced.
        """
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            raise PsCliError(msg=_AUTHENTICATION_REJECTED_MSG.format(base_url=self._base_url))

    def _get(self, path: str, *, timeout: httpx.Timeout | None = None) -> httpx.Response:
        """Un-authenticated `GET path` (D-57-6): no bearer header, no 401 special-case.

        Used only by `check_health`/`get_service_version`/`check_readiness` --
        endpoints exempt from the login-required contract. `timeout=None` (the
        default) uses the client-wide default timeout, matching every pre-#57
        call site that never passed an explicit `timeout` either.
        """
        try:
            if timeout is None:
                return self._client.get(path)
            return self._client.get(path, timeout=timeout)
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(self._base_url, exc)
        except httpx.TransportError as exc:
            _raise_connection_error(self._base_url, exc)

    def _post(
        self, path: str, *, json: object | None = None, timeout: httpx.Timeout | None = None
    ) -> httpx.Response:
        """Un-authenticated `POST path` (D-57-6): no bearer header, no 401 special-case."""
        try:
            if timeout is None:
                return self._client.post(path, json=json)
            return self._client.post(path, json=json, timeout=timeout)
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(self._base_url, exc)
        except httpx.TransportError as exc:
            _raise_connection_error(self._base_url, exc)

    def _authenticated_post(
        self, path: str, *, json: object | None = None, timeout: httpx.Timeout | None = None
    ) -> httpx.Response:
        """`POST path` with a bearer header attached when configured (AC-BI-011).

        Raises `PsCliError` on a connect failure/read timeout (unchanged wording),
        or on a 401 (AC-BI-014, before any other status check) -- callers still run
        their own `if not response.is_success: _raise_from_error_body(response)`
        check afterward for every other non-2xx status.
        """
        headers = self._authorization_headers()
        try:
            if timeout is None:
                response = self._client.post(path, json=json, headers=headers)
            else:
                response = self._client.post(path, json=json, headers=headers, timeout=timeout)
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(self._base_url, exc)
        except httpx.TransportError as exc:
            _raise_connection_error(self._base_url, exc)
        self._raise_if_unauthorized(response)
        return response

    def check_health(self) -> str:
        """`GET /health`: whether the ASGI server is accepting connections.

        Raises `PsCliError` if PS Service cannot be reached or the connection
        is interrupted (refused, reset, or timed out) or if the response body
        does not match the expected shape. Never checks external dependencies
        (D6) — a healthy result here does not imply `check_readiness()` will
        also succeed.
        """
        response = self._get(_HEALTH_PATH)
        return _parse_health_body(response.json())

    def get_service_version(self) -> str:
        """`GET /health`: PS Service's installed package version.

        Raises `PsCliError` if PS Service cannot be reached or the connection
        is interrupted (refused, reset, or timed out) or if the response body
        does not match the expected shape.
        """
        response = self._get(_HEALTH_PATH)
        return _parse_service_version_body(response.json())

    def check_readiness(self) -> ReadinessResult:
        """`GET /ready`: readiness plus any currently-unhealthy dependency names.

        Reports whether PS Service's startup checks completed, and which
        dependencies (if any) are currently recorded unhealthy.

        Raises `PsCliError` if PS Service cannot be reached or the connection
        is interrupted (refused, reset, or timed out) or if the response body
        does not match the expected shape.
        """
        response = self._get(_READY_PATH)
        return _parse_readiness_body(response.json())

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
        embeddings backfill, so this needs a longer budget than a bounded,
        no-LLM-call endpoint would.
        """
        response = self._authenticated_post(
            _EXPORTS_PATH,
            json={"instrument_id": instrument_id},
            timeout=_EXPORT_REQUEST_TIMEOUT,
        )
        if not response.is_success:
            _raise_from_error_body(response)
        return _parse_export_response(response.json())

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
        response = self._authenticated_post(
            _INGESTIONS_PATH,
            json={"source": "internal", "content": content},
            timeout=_INGESTION_REQUEST_TIMEOUT,
        )
        if not response.is_success:
            _raise_from_error_body(response)
        return _parse_ingestion_response(response.json())
