"""`PsServiceClient`: the sole channel `ps-cli` uses to reach PS Service, over REST.

Every network failure and every non-success response PS Service can return is
translated into `PsCliError` here (PLAN.md §1 D5) — command handlers and `cli.run()`
never see `httpx` exceptions directly.
"""

from __future__ import annotations

import sys
from typing import NoReturn, Protocol, cast
from urllib.parse import urlparse

import httpx

from ps_cli.errors import PsCliError
from ps_cli.models import IngestionResult, RegulationEntry, RegulationsResult, StageOutcome

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

# `POST /ingestions` blocks synchronously for the entire real pipeline (Ingestion ->
# Domain Mapper -> Company Merge, no async job queue, by #51's own design) -- a real CRA
# ingestion measured 612.86s (10m12s) end to end. The client-wide timeout (30s read) is
# correct for the fast, static `GET /regulations` call but far too short here, so this
# per-request override widens only the read timeout, only for `POST /ingestions`: 1800s
# (30 min) gives ~3x headroom over the observed real run for slower providers/larger
# regulations, while staying bounded (not infinite), per AC-BI-007's "actionable error,
# not a silent hang" intent for a genuinely stuck server. connect/write/pool stay at the
# fast client-wide 5s -- a slow *response* is expected for this endpoint, a slow
# *connection* is not. See OPEN_QUESTIONS_RESOLVED.md item 10 / PLAN.md's Increment 7
# AMENDMENT / briefs/BATCH_H_FIX.md.
_INGESTION_REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=1800.0, write=5.0, pool=5.0)


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


def _parse_regulation_entry(payload: object) -> RegulationEntry:
    """Parse one raw JSON object into a `RegulationEntry`.

    Raises `PsCliError` (generic, defensive — D5) if the shape does not match.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    celex = body.get("celex")
    title = body.get("title")
    if not isinstance(celex, str) or not isinstance(title, str):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    return RegulationEntry(celex=celex, title=title)


def _parse_regulations_body(payload: object) -> RegulationsResult:
    """Parse a `GET /regulations` 200 response body into a `RegulationsResult`.

    Raises `PsCliError` (generic, defensive — D5) if the body does not match the
    expected `RegulationCatalogResponse` shape.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    regulations_raw = body.get("regulations")
    run_id = body.get("run_id")
    if not isinstance(regulations_raw, list) or not isinstance(run_id, str):
        raise PsCliError(msg=_UNEXPECTED_RESPONSE_SHAPE_MSG)
    regulation_items = cast("list[object]", regulations_raw)
    regulations = [_parse_regulation_entry(item) for item in regulation_items]
    return RegulationsResult(regulations=regulations, run_id=run_id)


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


def _raise_from_error_body(response: httpx.Response) -> NoReturn:
    """Parse a non-2xx `POST /ingestions` response into `PsCliError` per D5's mapping table.

    Expects the structured `ErrorBody` shape (`{"error": {"code", "message",
    "failing_stage"}, "run_id"}`); falls back to a generic `PsCliError` naming
    the HTTP status if the body is not JSON or does not match that shape —
    never assume the server always returns the documented shape. Shared
    verbatim by `ingest_catalog()` and `ingest_internal()`.
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

    def list_regulations(self) -> RegulationsResult:
        """`GET /regulations`: the curated catalog of ingestible regulations."""
        ...

    def ingest_catalog(self, celex: str) -> IngestionResult:
        """`POST /ingestions` with `{"source": "catalog", "celex": celex}`."""
        ...

    def ingest_internal(self, fixture_path: str) -> IngestionResult:
        """`POST /ingestions` with `{"source": "internal", "fixture_path": fixture_path}`."""
        ...


class PsServiceClient:
    """Thin REST client over PS Service's `GET /regulations` / `POST /ingestions`."""

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

    def list_regulations(self) -> RegulationsResult:
        """`GET /regulations`: the curated catalog of ingestible regulations.

        Raises `PsCliError` if PS Service cannot be reached (connection refused
        or a connect timeout) or if the response body does not match the
        expected shape.
        """
        try:
            response = self._client.get("/regulations")
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            _raise_connection_error(self._base_url, exc)
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(self._base_url, exc)
        return _parse_regulations_body(response.json())

    def ingest_catalog(self, celex: str) -> IngestionResult:
        """`POST /ingestions` with `{"source": "catalog", "celex": celex}`.

        Ingests a curated EU regulation, identified by its CELEX identifier,
        into the graph. Raises `PsCliError` if PS Service cannot be reached, if
        it returns a non-2xx response (parsed per D5's error-body mapping —
        `failing_stage`, when present, is included in the raised message), or
        if a 200 response body does not match the expected success shape.
        """
        try:
            response = self._client.post(
                _INGESTIONS_PATH,
                json={"source": "catalog", "celex": celex},
                timeout=_INGESTION_REQUEST_TIMEOUT,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            _raise_connection_error(self._base_url, exc)
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(self._base_url, exc)
        if not response.is_success:
            _raise_from_error_body(response)
        return _parse_ingestion_response(response.json())

    def ingest_internal(self, fixture_path: str) -> IngestionResult:
        """`POST /ingestions` with `{"source": "internal", "fixture_path": fixture_path}`.

        Ingests an internal-document fixture, identified by a path resolved
        server-side against PS Service's own fixtures root (PLAN.md §1 D8 --
        `fixture_path` is never read from the local filesystem here). Raises
        `PsCliError` if PS Service cannot be reached, if it returns a non-2xx
        response (parsed per D5's error-body mapping -- today this always
        includes a 501 `internal_ingestion_not_implemented` until issue #54's
        backend lands), or if a 200 response body does not match the expected
        success shape.
        """
        try:
            response = self._client.post(
                _INGESTIONS_PATH,
                json={"source": "internal", "fixture_path": fixture_path},
                timeout=_INGESTION_REQUEST_TIMEOUT,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            _raise_connection_error(self._base_url, exc)
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(self._base_url, exc)
        if not response.is_success:
            _raise_from_error_body(response)
        return _parse_ingestion_response(response.json())
