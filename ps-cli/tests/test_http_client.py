"""Tests for ps_cli.http_client: PsServiceClient construction, insecure-URL warning,
and its various endpoint methods.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from ps_cli.credentials import TokenBundle
from ps_cli.errors import PsCliError
from ps_cli.http_client import (
    _UNEXPECTED_RESPONSE_SHAPE_MSG,  # pyright: ignore[reportPrivateUsage]  # asserted verbatim
    PsServiceClient,
    _should_warn_insecure,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly
)
from ps_cli.models import ServiceStatus, StatusDetail

if TYPE_CHECKING:
    from collections.abc import Callable

# Shared wire-contract literal : both ps-service's and
# ps-cli's own test suites read the internal-ingestion envelope's field name from
# this one file (canonically owned by ps-service, since it defines the internal
# envelope contract), so a one-sided rename of `content` breaks the *other* side's
# test rather than going unnoticed by either.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WIRE_CONTRACTS_DIR = _REPO_ROOT / "ps-service" / "tests" / "fixtures" / "wire-contracts"
_ENVELOPE_CONTRACT_PATH = _WIRE_CONTRACTS_DIR / "ingest-internal-envelope.json"
_ENVELOPE_CONTRACT: dict[str, object] = json.loads(_ENVELOPE_CONTRACT_PATH.read_text())
# The 409 body ps-service answers `POST /exports` with while the instrument has unresolved
# near-miss reviews; ps-service's own route test asserts its response equals this file.
_EXPORT_NEAR_MISSES_409_PATH = _WIRE_CONTRACTS_DIR / "export-unresolved-near-misses-409.json"
_EXPORT_NEAR_MISSES_409: dict[str, object] = json.loads(_EXPORT_NEAR_MISSES_409_PATH.read_text())


class TestShouldWarnInsecure:
    """Table-driven proof of the insecure-URL heuristic."""

    @pytest.mark.parametrize(
        ("url", "expect_warning"),
        [
            ("http://127.0.0.1:8000", False),
            ("http://example.com", True),
            ("https://example.com", False),
            ("https://127.0.0.1:8000", False),
            ("http://localhost:8000", False),
            ("http://[::1]:8000", False),
        ],
    )
    def test_matches_expected_warning(self, *, url: str, expect_warning: bool) -> None:
        """Warn iff scheme != https AND hostname not in the loopback spelling set."""
        assert _should_warn_insecure(url) is expect_warning


class TestPsServiceClientConstruction:
    """The insecure-URL warning is printed to stderr, once, at construction."""

    def test_insecure_non_loopback_url_prints_warning_to_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A non-https, non-loopback base_url prints exactly one warning to stderr."""
        PsServiceClient("http://example.com", transport=httpx.MockTransport(_unused_handler))

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "example.com" in captured.err
        assert captured.err.count("\n") == 1

    def test_loopback_http_url_prints_no_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A plain http:// loopback base_url is not warned about."""
        PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_unused_handler))

        captured = capsys.readouterr()
        assert captured.err == ""

    def test_https_url_prints_no_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        """An https:// base_url is never warned about, loopback or not."""
        PsServiceClient("https://example.com", transport=httpx.MockTransport(_unused_handler))

        captured = capsys.readouterr()
        assert captured.err == ""


def _unused_handler(request: httpx.Request) -> httpx.Response:
    """A transport handler that should never be invoked (construction-only tests)."""
    msg = f"unexpected request in a construction-only test: {request.url}"
    raise AssertionError(msg)


def _connect_error_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _read_error_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadError("[Errno 54] Connection reset by peer", request=request)


def _make_error_body_handler(
    *, status_code: int, code: str, message: str, failing_stage: str | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            status_code,
            json={
                "error": {"code": code, "message": message, "failing_stage": failing_stage},
                "run_id": "run-ingest-err",
            },
        )

    return _handler


_INTERNAL_INGESTION_SUCCESS_BODY = {
    "run_id": "run-internal-001",
    "regulatory_instrument_id": "ri-internal",
    "source": "internal",
    "stages": [
        {"stage": "parse", "status": "succeeded", "summary": {"nodes": 1}},
    ],
}

# The exact, current string from ps_service/api/routes.py's
# _INTERNAL_NOT_IMPLEMENTED_MESSAGE constant -- confirmed by reading that file,
# not a live call (read-only reference; ps-cli never imports ps_service).
_INTERNAL_NOT_IMPLEMENTED_MESSAGE = (
    "Internal-document ingestion is not implemented in this walking-skeleton "
    "release; it is tracked in issue #54 (mindovermachine-dev/policy-system)."
)


def _internal_ingestion_success_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/ingestions"
    assert request.method == "POST"
    return httpx.Response(200, json=_INTERNAL_INGESTION_SUCCESS_BODY)


def _internal_not_implemented_handler(request: httpx.Request) -> httpx.Response:
    del request
    return httpx.Response(
        501,
        json={
            "error": {
                "code": "internal_ingestion_not_implemented",
                "message": _INTERNAL_NOT_IMPLEMENTED_MESSAGE,
                "failing_stage": None,
            },
            "run_id": "run-internal-501",
        },
    )


# 200 bodies that are not JSON: every success-body parse must turn these into the
# body-shape `PsCliError`, never let `ValueError` escape.
_NON_JSON_200_BODIES = [
    pytest.param(b"<html><body>PS Service</body></html>", id="html"),
    pytest.param(b"", id="empty"),
    pytest.param(b"\xff\xfe\xfa not utf-8", id="undecodable"),
    pytest.param(b'{"run_id": "run-1", ', id="truncated-json"),
]


def _non_json_200_handler(content: bytes) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=content, headers={"content-type": "text/html"})

    return _handler


class TestIngestInternal:
    """PsServiceClient.ingest_internal(content)."""

    def test_parses_a_200_success_response(self) -> None:
        """A 200 POST /ingestions body with source "internal" parses into an IngestionResult."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(_internal_ingestion_success_handler),
        )

        result = client.ingest_internal({"nodes": [], "edges": []})

        assert result.run_id == "run-internal-001"
        assert result.regulatory_instrument_id == "ri-internal"
        assert result.source == "internal"
        assert len(result.stages) == 1
        assert result.stages[0].stage == "parse"
        assert result.stages[0].status == "succeeded"
        assert result.stages[0].summary == {"nodes": 1}

    def test_posts_the_expected_request_body(self) -> None:
        """The request body is {"source": "internal", <content_field_name>: content}."""
        captured_bodies: list[object] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(json.loads(request.content))
            return httpx.Response(200, json=_INTERNAL_INGESTION_SUCCESS_BODY)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))
        content: dict[str, object] = {"nodes": [], "edges": []}

        client.ingest_internal(content)

        assert captured_bodies == [
            {
                "source": _ENVELOPE_CONTRACT["source"],
                _ENVELOPE_CONTRACT["content_field_name"]: content,
            }
        ]

    @pytest.mark.parametrize("content", _NON_JSON_200_BODIES)
    def test_200_with_a_non_json_body_raises_the_body_shape_error(self, content: bytes) -> None:
        """A 200 whose body is not JSON raises the body-shape error, never `ValueError`."""
        client = PsServiceClient(
            _SERVICE_URL, transport=httpx.MockTransport(_non_json_200_handler(content))
        )

        with pytest.raises(PsCliError) as excinfo:
            client.ingest_internal({"nodes": [], "edges": []})

        assert excinfo.value.msg == _UNEXPECTED_RESPONSE_SHAPE_MSG

    def test_501_internal_ingestion_not_implemented_raises_ps_cli_error(self) -> None:
        """The exact 501 body a real, unmodified ps-service returns today for this call.

        Confirmed by reading ps_service/api/routes.py's
        _INTERNAL_NOT_IMPLEMENTED_MESSAGE constant and ps_service/api/
        error_handlers.py's _API_ERROR_SPECS entry for
        InternalIngestionNotImplementedError -- read-only reference, this test
        does not import from ps_service. Asserts PsCliError carries that exact
        message and the run_id.
        """
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(_internal_not_implemented_handler),
        )

        with pytest.raises(PsCliError) as excinfo:
            client.ingest_internal({"nodes": [], "edges": []})

        assert "internal_ingestion_not_implemented" in excinfo.value.msg
        assert _INTERNAL_NOT_IMPLEMENTED_MESSAGE in excinfo.value.msg
        assert excinfo.value.hint is not None
        assert "run-internal-501" in excinfo.value.hint

    def test_posts_with_the_extended_ingestion_read_timeout(self) -> None:
        """OPEN_QUESTIONS_RESOLVED.md item 10 / BATCH_H_FIX.md.

        `POST /ingestions` blocks synchronously for the whole real pipeline (a real CRA
        ingestion measured 612.86s / 10m12s) -- the client-wide 30s read timeout is far
        too short for it. This proves `ingest_internal()` passes a per-request override
        widening only the read timeout to 1800s (30 min); connect/write/pool stay at the
        fast client-wide 5s, since a slow *response* is expected here but a slow
        *connection* is not.
        """
        captured_timeouts: list[object] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured_timeouts.append(request.extensions.get("timeout"))
            return httpx.Response(200, json=_INTERNAL_INGESTION_SUCCESS_BODY)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        client.ingest_internal({"nodes": [], "edges": []})

        assert captured_timeouts == [{"connect": 5.0, "read": 1800.0, "write": 5.0, "pool": 5.0}]


_EXPORT_MANIFEST_BODY = {
    "instrument_id": "CRA-1.0",
    "celex": "32024R2847",
    "title": "Cyber Resilience Act",
    "short_name": "CRA",
    "version": "1.0",
    "source_type": "external",
    "jurisdiction": "EU",
    "schema_version": "1.0.0",
    "exported_at": "2026-09-04T00:00:00Z",
    "baseline_sha256": "a" * 64,
    "native_sha256": "b" * 64,
}

_EXPORT_SUCCESS_BODY = {
    "instrument_id": "CRA-1.0",
    "manifest": _EXPORT_MANIFEST_BODY,
    "baseline_blob_base64": base64.b64encode(b'{"nodes": [], "edges": []}').decode("ascii"),
    "native_blob_base64": base64.b64encode(b'{"nodes": [], "edges": []}').decode("ascii"),
    "stages": [
        {"stage": "serialized", "status": "succeeded"},
    ],
}


def _export_success_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/exports"
    assert request.method == "POST"
    return httpx.Response(200, json=_EXPORT_SUCCESS_BODY)


class TestExportInstrument:
    """PsServiceClient.export_instrument(instrument_id)."""

    def test_parses_a_200_success_response(self) -> None:
        """A 200 POST /exports body parses into an ExportResult."""
        client = PsServiceClient(
            "http://127.0.0.1:8000", transport=httpx.MockTransport(_export_success_handler)
        )

        result = client.export_instrument("CRA-1.0")

        assert result.instrument_id == "CRA-1.0"
        assert result.manifest.instrument_id == "CRA-1.0"
        assert result.manifest.celex == "32024R2847"
        assert result.manifest.source_type == "external"
        assert result.manifest.baseline_sha256 == "a" * 64
        assert result.baseline_blob_base64 == _EXPORT_SUCCESS_BODY["baseline_blob_base64"]
        assert result.native_blob_base64 == _EXPORT_SUCCESS_BODY["native_blob_base64"]
        assert len(result.stages) == 1
        assert result.stages[0].stage == "serialized"
        assert result.stages[0].status == "succeeded"

    def test_posts_the_expected_request_body(self) -> None:
        """The request body is {"instrument_id": instrument_id}, posted to /exports."""
        captured_bodies: list[object] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/exports"
            captured_bodies.append(json.loads(request.content))
            return httpx.Response(200, json=_EXPORT_SUCCESS_BODY)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        client.export_instrument("CRA-1.0")

        assert captured_bodies == [{"instrument_id": "CRA-1.0"}]

    @pytest.mark.parametrize("content", _NON_JSON_200_BODIES)
    def test_200_with_a_non_json_body_raises_the_body_shape_error(self, content: bytes) -> None:
        """A 200 whose body is not JSON raises the body-shape error, never `ValueError`."""
        client = PsServiceClient(
            _SERVICE_URL, transport=httpx.MockTransport(_non_json_200_handler(content))
        )

        with pytest.raises(PsCliError) as excinfo:
            client.export_instrument("CRA-1.0")

        assert excinfo.value.msg == _UNEXPECTED_RESPONSE_SHAPE_MSG

    def test_404_export_instrument_not_found_raises_ps_cli_error(self) -> None:
        """A 404 export_instrument_not_found body maps to PsCliError.

        Exercises the *existing*, unmodified `_raise_from_error_body` -- proves
        no new client-side classification code is needed for export's error shapes.
        """
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(
                _make_error_body_handler(
                    status_code=404,
                    code="export_instrument_not_found",
                    message="no ingested instrument with id 'MISSING-1.0'",
                )
            ),
        )

        with pytest.raises(PsCliError) as excinfo:
            client.export_instrument("MISSING-1.0")

        assert "export_instrument_not_found" in excinfo.value.msg
        assert "MISSING-1.0" in excinfo.value.msg

    def test_409_export_unresolved_near_misses_surfaces_code_count_and_review_ids(self) -> None:
        """PIN (characterisation): the service's 409 body, read from the shared wire contract.

        The generic `_raise_from_error_body` already carries the code and the service's message
        (the count and every review id) into the raised error, so no client change is needed.
        """

        def _handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(409, json=_EXPORT_NEAR_MISSES_409)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        with pytest.raises(PsCliError) as excinfo:
            client.export_instrument("CRA-1.0")

        assert "export_unresolved_near_misses" in excinfo.value.msg
        assert "2 unresolved near-miss reviews" in excinfo.value.msg
        assert "review_1" in excinfo.value.msg
        assert "review_2" in excinfo.value.msg

    def test_502_export_stage_failed_surfaces_failing_stage(self) -> None:
        """A 502 export_stage_failed body's failing_stage surfaces in the raised error."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(
                _make_error_body_handler(
                    status_code=502,
                    code="export_stage_failed",
                    message="the serialize stage failed",
                    failing_stage="serialize",
                )
            ),
        )

        with pytest.raises(PsCliError) as excinfo:
            client.export_instrument("CRA-1.0")

        assert "export_stage_failed" in excinfo.value.msg
        assert "serialize" in excinfo.value.msg

    def test_503_export_config_incomplete_raises_ps_cli_error(self) -> None:
        """A 503 export_config_incomplete body maps to PsCliError."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(
                _make_error_body_handler(
                    status_code=503,
                    code="export_config_incomplete",
                    message="LLM Interface embedding model is not configured.",
                )
            ),
        )

        with pytest.raises(PsCliError) as excinfo:
            client.export_instrument("CRA-1.0")

        assert "export_config_incomplete" in excinfo.value.msg

    def test_connect_error_raises_ps_cli_error_with_actionable_message(self) -> None:
        """A transport-level ConnectError maps to PsCliError."""
        client = PsServiceClient(
            "http://127.0.0.1:8000", transport=httpx.MockTransport(_connect_error_handler)
        )

        with pytest.raises(PsCliError) as excinfo:
            client.export_instrument("CRA-1.0")

        assert "Could not reach PS Service" in excinfo.value.msg

    def test_read_timeout_raises_ps_cli_error(self) -> None:
        """A transport-level ReadTimeout maps to PsCliError."""

        def _handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        with pytest.raises(PsCliError) as excinfo:
            client.export_instrument("CRA-1.0")

        assert "did not respond in time" in excinfo.value.msg

    def test_posts_with_the_extended_export_read_timeout(self) -> None:
        """export_instrument() passes the extended 1800s read timeout, not restore's 300s.

        Export always runs a real LLM embeddings backfill, unlike restore's dedup
        replay which reuses the artifact's own embeddings.
        """
        captured_timeouts: list[object] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured_timeouts.append(request.extensions.get("timeout"))
            return httpx.Response(200, json=_EXPORT_SUCCESS_BODY)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        client.export_instrument("CRA-1.0")

        assert captured_timeouts == [{"connect": 5.0, "read": 1800.0, "write": 5.0, "pool": 5.0}]


_STATUS_PATH = "/status"
_SERVICE_URL = "http://127.0.0.1:8000"
_STATUS_BODY_ANONYMOUS: dict[str, object] = {"status": "ready", "version": "1.4.0"}
_STATUS_BODY_DETAIL_READY: dict[str, object] = {
    "status": "ready",
    "version": "1.4.0",
    "unhealthy_dependencies": [],
    "gated_graphs": [],
}
_STATUS_BODY_DETAIL_NOT_READY: dict[str, object] = {
    "status": "not_ready",
    "version": "1.4.0",
    "unhealthy_dependencies": ["falkordb", "cellar_eli"],
    "gated_graphs": ["policy_system"],
}
# The status and probe paths PS Service serves, plus the two probe paths it does not serve,
# so a test can assert which of them the client requests.
_STATUS_AND_PROBE_PATHS = frozenset(
    {"/status", "/livez", "/readyz", *(f"/{name}" for name in ("health", "ready"))}
)
_INCOMPATIBLE_SERVICE_WORDING = (
    "the URL may not point to PS Service, or this ps-cli and PS Service versions are incompatible"
)


def _status_requests(recorded: list[httpx.Request]) -> list[tuple[str, str]]:
    """The `(method, path)` of every recorded request to a status or probe path, in order."""
    return [(r.method, r.url.path) for r in recorded if r.url.path in _STATUS_AND_PROBE_PATHS]


def _make_status_handler(
    body: object, *, recorded: list[httpx.Request] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(request: httpx.Request) -> httpx.Response:
        if recorded is not None:
            recorded.append(request)
        assert request.url.path == _STATUS_PATH
        assert request.method == "GET"
        return httpx.Response(200, json=body)

    return _handler


def _anonymous_client(handler: Callable[[httpx.Request], httpx.Response]) -> PsServiceClient:
    return PsServiceClient(_SERVICE_URL, transport=httpx.MockTransport(handler))


class TestGetStatus:
    """`PsServiceClient.get_status()`: one status-checked `GET /status`."""

    def test_anonymous_reply_parses_with_no_detail(self) -> None:
        client = _anonymous_client(_make_status_handler(_STATUS_BODY_ANONYMOUS))

        result = client.get_status(detail=StatusDetail.NONE)

        assert result == ServiceStatus(
            status="ready", version="1.4.0", unhealthy_dependencies=None, gated_graphs=None
        )

    def test_detail_reply_parses_the_dependency_names_and_gated_graphs(self) -> None:
        client = _anonymous_client(_make_status_handler(_STATUS_BODY_DETAIL_NOT_READY))

        result = client.get_status()

        assert result == ServiceStatus(
            status="not_ready",
            version="1.4.0",
            unhealthy_dependencies=["falkordb", "cellar_eli"],
            gated_graphs=["policy_system"],
        )

    @pytest.mark.parametrize("detail", list(StatusDetail))
    def test_each_detail_mode_sends_exactly_one_get_status(self, detail: StatusDetail) -> None:
        """Whatever the detail mode, the only status request is one `GET /status`.

        Every request the client makes is recorded, including the login's discovery
        and token calls, which are authentication rather than status requests.
        """
        recorded: list[httpx.Request] = []
        client = _logged_in_client(
            _make_status_handler(_STATUS_BODY_DETAIL_READY), recorded=recorded
        )

        client.get_status(detail=detail)

        assert _status_requests(recorded) == [("GET", _STATUS_PATH)]

    @pytest.mark.parametrize("status_code", [404, 401, 500, 503])
    @pytest.mark.parametrize(
        "response_kwargs",
        [
            pytest.param({"json": {"detail": "Not Found"}}, id="json-body"),
            pytest.param(
                {
                    "text": "<html><body><h1>Not Found</h1></body></html>",
                    "headers": {"content-type": "text/html"},
                },
                id="html-page",
            ),
        ],
    )
    def test_non_200_raises_naming_method_path_and_status(
        self, status_code: int, response_kwargs: dict[str, object]
    ) -> None:
        """Any status other than 200 names the request and the status, never "unreachable"."""

        def _handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(status_code, **response_kwargs)  # pyright: ignore[reportArgumentType]  # kwargs are the two literal shapes parametrized above

        client = _anonymous_client(_handler)

        with pytest.raises(PsCliError) as excinfo:
            client.get_status()

        message = excinfo.value.msg
        rendered = f"{message} {excinfo.value.hint or ''}".lower()
        assert message == (
            f"PS Service at {_SERVICE_URL} answered GET /status with HTTP {status_code}: "
            f"{_INCOMPATIBLE_SERVICE_WORDING}."
        )
        assert "GET /status" in message
        assert f"HTTP {status_code}" in message
        assert _INCOMPATIBLE_SERVICE_WORDING in message
        assert excinfo.value.hint is None
        assert "unreachable" not in rendered
        for word in ("livez", "readyz", "renamed", "rename"):
            assert word not in rendered

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(b"<html><body>PS Service</body></html>", id="html"),
            pytest.param(b"", id="empty"),
            pytest.param(b"\xff\xfe\xfa not utf-8", id="undecodable"),
            pytest.param(b'{"status": "ready", ', id="truncated-json"),
        ],
    )
    def test_200_with_a_non_json_body_raises_the_body_shape_error(self, content: bytes) -> None:
        """A 200 whose body is not JSON raises the body-shape error, never `ValueError`."""

        def _handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, content=content, headers={"content-type": "text/html"})

        client = _anonymous_client(_handler)

        with pytest.raises(PsCliError) as excinfo:
            client.get_status()

        assert excinfo.value.msg == _UNEXPECTED_RESPONSE_SHAPE_MSG

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(["not", "a", "dict"], id="non-dict"),
            pytest.param({"version": "1.4.0"}, id="missing-status"),
            pytest.param({"status": 123, "version": "1.4.0"}, id="non-str-status"),
            pytest.param({"status": "ready"}, id="missing-version"),
            pytest.param({"status": "ready", "version": 1}, id="non-str-version"),
            pytest.param(
                {"status": "ready", "version": "1.4.0", "unhealthy_dependencies": "falkordb"},
                id="unhealthy-not-a-list",
            ),
            pytest.param(
                {"status": "ready", "version": "1.4.0", "unhealthy_dependencies": ["x", 1]},
                id="unhealthy-non-str-item",
            ),
            pytest.param(
                {"status": "ready", "version": "1.4.0", "gated_graphs": "policy_system"},
                id="gated-not-a-list",
            ),
            pytest.param(
                {"status": "ready", "version": "1.4.0", "gated_graphs": [None]},
                id="gated-non-str-item",
            ),
        ],
    )
    def test_200_with_an_unexpected_shape_raises_the_body_shape_error(self, body: object) -> None:
        client = _anonymous_client(lambda request: httpx.Response(200, json=body))

        with pytest.raises(PsCliError) as excinfo:
            client.get_status()

        assert excinfo.value.msg == _UNEXPECTED_RESPONSE_SHAPE_MSG

    @pytest.mark.parametrize("handler", [_connect_error_handler, _read_error_handler])
    def test_transport_failure_raises_the_could_not_reach_error(
        self, handler: Callable[[httpx.Request], httpx.Response]
    ) -> None:
        """A refused or reset connection maps to the actionable reachability error."""
        client = _anonymous_client(handler)

        with pytest.raises(PsCliError) as excinfo:
            client.get_status(detail=StatusDetail.NONE)

        assert "Could not reach PS Service at" in excinfo.value.msg
        assert _SERVICE_URL in excinfo.value.msg
        assert excinfo.value.hint is not None
        assert "PS_CLI_SERVICE_URL" in excinfo.value.hint


# --- token use, refresh-once -----------------------------------------------------
#
# `PsServiceClient` threads its own `transport` constructor argument into
# `device_flow.ensure_valid_access_token` too (`self._transport`), so one
# `httpx.MockTransport` can answer PS Service's resource-metadata endpoint, the fake
# issuer's openid-configuration and token endpoints, *and* PS Service's own business
# endpoint -- a genuine wire-level fake, not a `resolve_auth_parameters` monkeypatch,
# with a refresh-call-counting fake transport.

_FAKE_ISSUER = "https://issuer.example"
_FAKE_CLIENT_ID = "ps-cli-test-client"
_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
_OPENID_CONFIGURATION_URL = f"{_FAKE_ISSUER}/.well-known/openid-configuration"
_TOKEN_URL = f"{_FAKE_ISSUER}/token"


class _FakeCredentialStore:
    """A minimal dict-backed `CredentialStore` double for this group's tests.

    Structural match only (no inheritance) -- mirrors this repo's own precedent
    for a hand-written `CredentialStore` double (`test_config_handlers.py`'s
    `_RecordingCredentialStore`).
    """

    def __init__(self) -> None:
        """Start with no tokens stored for any context."""
        self._tokens: dict[str, TokenBundle] = {}

    def get_tokens(self, context: str) -> TokenBundle | None:
        """Return the stored `TokenBundle` for `context`, or `None` if none is stored."""
        return self._tokens.get(context)

    def set_tokens(self, context: str, tokens: TokenBundle) -> None:
        """Store `tokens` for `context`, overwriting any existing value."""
        self._tokens[context] = tokens

    def delete_tokens(self, context: str) -> None:
        """Remove `context`'s stored token bundle; a no-op if none exists."""
        self._tokens.pop(context, None)


def _build_auth_and_business_transport(
    business_handler: Callable[[httpx.Request], httpx.Response],
    *,
    refresh_response: Callable[[int], httpx.Response] | None = None,
    recorded: list[httpx.Request] | None = None,
) -> tuple[httpx.MockTransport, list[int]]:
    """One fake transport answering resource-metadata/discovery/refresh + business calls.

    `refresh_response`, when given, is called with the 1-based call number on every
    `POST <issuer>/token` and its return value is sent back verbatim -- lets a test
    simulate a refresh rejection. Defaults to minting `refreshed-token-<n>` on success.
    `recorded`, when given, receives every request the transport sees, authentication
    calls included, in order.
    Returns `(transport, call_count)` where `call_count[0]` is mutated on every refresh
    call, so a test can assert exactly how many refreshes happened.
    """
    call_count = [0]

    def _handle(request: httpx.Request) -> httpx.Response:
        if recorded is not None:
            recorded.append(request)
        if request.url.path == _RESOURCE_METADATA_PATH:
            return httpx.Response(
                200,
                json={
                    "resource": "http://127.0.0.1:8000",
                    "authorization_servers": [_FAKE_ISSUER],
                    "scopes_supported": ["openid"],
                    "ps_cli_client_id": _FAKE_CLIENT_ID,
                },
            )
        if str(request.url) == _OPENID_CONFIGURATION_URL:
            return httpx.Response(
                200,
                json={
                    "issuer": _FAKE_ISSUER,
                    "device_authorization_endpoint": f"{_FAKE_ISSUER}/device_authorization",
                    "token_endpoint": _TOKEN_URL,
                },
            )
        if str(request.url) == _TOKEN_URL:
            call_count[0] += 1
            if refresh_response is not None:
                return refresh_response(call_count[0])
            return httpx.Response(
                200,
                json={
                    "access_token": f"refreshed-token-{call_count[0]}",
                    "refresh_token": "rt-rotated",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        return business_handler(request)

    return httpx.MockTransport(_handle), call_count


class TestAuthenticatedRequestsAttachBearerHeader:
    """Exactly one refresh per invocation, then reused."""

    def test_ingest_internal_attaches_authorization_header_from_a_freshly_refreshed_token(
        self,
    ) -> None:
        """`credential_store`+`context` given -> a refresh happens, and the resulting
        access token is attached as `Authorization: Bearer`.
        """
        captured_headers: list[str | None] = []

        def _business_handler(request: httpx.Request) -> httpx.Response:
            captured_headers.append(request.headers.get("authorization"))
            return httpx.Response(200, json=_INTERNAL_INGESTION_SUCCESS_BODY)

        transport, refresh_calls = _build_auth_and_business_transport(_business_handler)
        store = _FakeCredentialStore()
        store.set_tokens("dev", TokenBundle(refresh_token="seed-rt", issuer=_FAKE_ISSUER))
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=transport,
            credential_store=store,
            context="dev",
        )

        client.ingest_internal({"nodes": [], "edges": []})

        assert captured_headers == ["Bearer refreshed-token-1"]
        assert refresh_calls == [1]

    def test_two_authenticated_calls_on_the_same_client_share_exactly_one_refresh(
        self,
    ) -> None:
        """Core proof: a second authenticated call on the *same*
        `PsServiceClient` instance reuses the in-memory `AccessTokenCache` -- the fake
        refresh endpoint is called exactly once across both calls, and both requests
        carry the identical bearer token.
        """
        captured_headers: list[str | None] = []

        def _business_handler(request: httpx.Request) -> httpx.Response:
            captured_headers.append(request.headers.get("authorization"))
            return httpx.Response(200, json=_INTERNAL_INGESTION_SUCCESS_BODY)

        transport, refresh_calls = _build_auth_and_business_transport(_business_handler)
        store = _FakeCredentialStore()
        store.set_tokens("dev", TokenBundle(refresh_token="seed-rt", issuer=_FAKE_ISSUER))
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=transport,
            credential_store=store,
            context="dev",
        )

        client.ingest_internal({"nodes": [], "edges": []})
        client.ingest_internal({"nodes": [], "edges": []})

        assert refresh_calls == [1]
        assert captured_headers == ["Bearer refreshed-token-1", "Bearer refreshed-token-1"]

    def test_construction_with_no_auth_params_is_byte_for_byte_unaffected(self) -> None:
        """The pre-authentication construction shapes still work: no header is ever attached.

        Proves the load-bearing backward-compatibility property: every existing
        construction site (`PsServiceClient(base_url)`,
        `PsServiceClient(base_url, transport=...)`) is unaffected by the optional
        auth-related parameters.
        """
        captured_headers: list[str | None] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured_headers.append(request.headers.get("authorization"))
            return httpx.Response(200, json=_INTERNAL_INGESTION_SUCCESS_BODY)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        client.ingest_internal({"nodes": [], "edges": []})

        assert captured_headers == [None]


class TestAuthenticationFailsClosed:
    """No-refresh-token / refresh-fails fail closed."""

    def test_ingest_internal_with_no_stored_credential_raises_without_sending_request(
        self,
    ) -> None:
        """No stored bundle at all -> fail closed; the PS Service transport is never touched."""
        store = _FakeCredentialStore()
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(_unused_handler),
            credential_store=store,
            context="dev",
        )

        with pytest.raises(PsCliError) as excinfo:
            client.ingest_internal({"nodes": [], "edges": []})

        assert "no stored credentials for context 'dev'" in excinfo.value.msg
        assert "ps-cli auth login" in (excinfo.value.hint or "")

    def test_ingest_internal_with_no_refresh_token_raises_without_sending_request(
        self,
    ) -> None:
        """A stored bundle with `refresh_token=None` -> fail closed, no request sent."""
        store = _FakeCredentialStore()
        store.set_tokens("dev", TokenBundle(refresh_token=None, issuer=_FAKE_ISSUER))
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(_unused_handler),
            credential_store=store,
            context="dev",
        )

        with pytest.raises(PsCliError) as excinfo:
            client.ingest_internal({"nodes": [], "edges": []})

        assert excinfo.value.msg == "stored credentials could not be refreshed"
        assert "ps-cli auth login" in (excinfo.value.hint or "")

    def test_ingest_internal_with_rejected_refresh_raises_without_sending_a_business_request(
        self,
    ) -> None:
        """A refresh_token the fake issuer rejects (`invalid_grant`) surfaces as the
        same fail-closed error, and PS Service's own business endpoint is never
        touched (`_unused_handler` fails the test if reached) -- proving the
        underlying `httpx.Client` call to PS Service is skipped entirely once the
        refresh attempt itself fails.
        """

        def _reject_refresh(call_number: int) -> httpx.Response:
            del call_number
            return httpx.Response(400, json={"error": "invalid_grant"})

        transport, refresh_calls = _build_auth_and_business_transport(
            _unused_handler, refresh_response=_reject_refresh
        )
        store = _FakeCredentialStore()
        store.set_tokens("dev", TokenBundle(refresh_token="stale-rt", issuer=_FAKE_ISSUER))
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=transport,
            credential_store=store,
            context="dev",
        )

        with pytest.raises(PsCliError) as excinfo:
            client.ingest_internal({"nodes": [], "edges": []})

        assert excinfo.value.msg == "stored credentials could not be refreshed"
        assert "ps-cli auth login" in (excinfo.value.hint or "")
        assert refresh_calls == [1]


class TestUnauthorizedResponseMapping:
    """A 401 maps to one actionable error, never the body."""

    def test_ingest_internal_maps_401_response_to_actionable_error_not_generic_http_error(
        self,
    ) -> None:
        """A bare 401, with PS Service's own structured error body attached, still maps
        to exactly the actionable 401 wording -- the body's own `code`/`message` never leak
        into `.msg` or `.hint`, proving the 401 check runs before
        `_raise_from_error_body` would otherwise parse and surface that body.
        """

        def _handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                401,
                json={
                    "error": {
                        "code": "invalid_token",
                        "message": "the access token is expired or invalid",
                        "failing_stage": None,
                    },
                    "run_id": None,
                },
            )

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        with pytest.raises(PsCliError) as excinfo:
            client.ingest_internal({"nodes": [], "edges": []})

        assert (
            excinfo.value.msg
            == "authentication rejected by http://127.0.0.1:8000; run `ps-cli auth login`"
        )
        assert "invalid_token" not in excinfo.value.msg
        assert "the access token is expired or invalid" not in excinfo.value.msg
        assert excinfo.value.hint is None

    def test_401_error_never_contains_the_bearer_token_value_that_was_sent(self) -> None:
        """A marker access token, already attached as
        the request's own bearer header when the 401 comes back, never leaks into
        `.msg`/`.hint` -- a proof pass over the existing code, not a new
        behavior.
        """
        marker_access_token = "marker-access-token-should-never-print-79c3"

        def _business_handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(401, json={"error": {"code": "invalid_token"}})

        def _mint_marker(call_number: int) -> httpx.Response:
            del call_number
            return httpx.Response(
                200,
                json={
                    "access_token": marker_access_token,
                    "refresh_token": "rt-rotated",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )

        transport, _ = _build_auth_and_business_transport(
            _business_handler, refresh_response=_mint_marker
        )
        store = _FakeCredentialStore()
        store.set_tokens("dev", TokenBundle(refresh_token="seed-rt", issuer=_FAKE_ISSUER))
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=transport,
            credential_store=store,
            context="dev",
        )

        with pytest.raises(PsCliError) as excinfo:
            client.ingest_internal({"nodes": [], "edges": []})

        assert marker_access_token not in excinfo.value.msg
        assert marker_access_token not in (excinfo.value.hint or "")


def _logged_in_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    recorded: list[httpx.Request] | None = None,
) -> PsServiceClient:
    transport, _ = _build_auth_and_business_transport(handler, recorded=recorded)
    store = _FakeCredentialStore()
    store.set_tokens("dev", TokenBundle(refresh_token="seed-rt", issuer=_FAKE_ISSUER))
    return PsServiceClient(_SERVICE_URL, transport=transport, credential_store=store, context="dev")


def _not_logged_in_client(handler: Callable[[httpx.Request], httpx.Response]) -> PsServiceClient:
    transport, _ = _build_auth_and_business_transport(handler)
    return PsServiceClient(
        _SERVICE_URL,
        transport=transport,
        credential_store=_FakeCredentialStore(),
        context="dev",
    )


class TestGetStatusDetail:
    """`get_status` with each `StatusDetail` mode, logged in and not."""

    def test_no_detail_never_reads_the_login_or_sends_a_token(self) -> None:
        """`StatusDetail.NONE` sends no header and makes no discovery or refresh call."""
        recorded: list[httpx.Request] = []
        captured: list[str | None] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.headers.get("authorization"))
            return httpx.Response(200, json=_STATUS_BODY_ANONYMOUS)

        transport, refresh_calls = _build_auth_and_business_transport(_handler, recorded=recorded)
        store = _FakeCredentialStore()
        store.set_tokens("dev", TokenBundle(refresh_token="seed-rt", issuer=_FAKE_ISSUER))
        client = PsServiceClient(
            _SERVICE_URL, transport=transport, credential_store=store, context="dev"
        )

        result = client.get_status(detail=StatusDetail.NONE)

        assert captured == [None]
        assert refresh_calls == [0]
        assert [(r.method, r.url.path) for r in recorded] == [("GET", _STATUS_PATH)]
        assert result.version == "1.4.0"

    def test_required_detail_raises_when_the_reply_carries_no_dependency_names(self) -> None:
        client = _logged_in_client(_make_status_handler(_STATUS_BODY_ANONYMOUS))

        with pytest.raises(PsCliError) as excinfo:
            client.get_status(detail=StatusDetail.REQUIRED)

        assert excinfo.value.msg == (
            "status detail unavailable: PS Service did not name its dependencies."
        )
        assert excinfo.value.hint == "run `ps-cli auth login` and retry"

    def test_required_detail_without_credentials_configured_raises_and_sends_nothing(
        self,
    ) -> None:
        requests_seen: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            requests_seen.append(request)
            return httpx.Response(200, json=_STATUS_BODY_DETAIL_READY)

        client = _anonymous_client(_handler)

        with pytest.raises(PsCliError) as excinfo:
            client.get_status(detail=StatusDetail.REQUIRED)

        assert excinfo.value.msg == "status detail unavailable: log in first."
        assert excinfo.value.hint == "run `ps-cli auth login` and retry"
        assert requests_seen == []

    def test_required_detail_when_not_logged_in_raises_and_never_asks_for_status(
        self,
    ) -> None:
        requests_seen: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            requests_seen.append(request.url.path)
            return httpx.Response(200, json=_STATUS_BODY_DETAIL_READY)

        client = _not_logged_in_client(_handler)

        with pytest.raises(PsCliError):
            client.get_status(detail=StatusDetail.REQUIRED)

        assert _STATUS_PATH not in requests_seen

    def test_required_detail_sends_the_bearer_token_and_returns_the_names(self) -> None:
        captured: list[str | None] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.headers.get("authorization"))
            return httpx.Response(200, json=_STATUS_BODY_DETAIL_NOT_READY)

        client = _logged_in_client(_handler)

        result = client.get_status(detail=StatusDetail.REQUIRED)

        assert captured == ["Bearer refreshed-token-1"]
        assert result.unhealthy_dependencies == ["falkordb", "cellar_eli"]

    def test_default_call_when_logged_in_sends_the_bearer_token_and_returns_the_names(
        self,
    ) -> None:
        captured: list[str | None] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.headers.get("authorization"))
            return httpx.Response(200, json=_STATUS_BODY_DETAIL_NOT_READY)

        client = _logged_in_client(_handler)

        result = client.get_status()

        assert captured == ["Bearer refreshed-token-1"]
        assert result.unhealthy_dependencies == ["falkordb", "cellar_eli"]
        assert result.gated_graphs == ["policy_system"]

    def test_default_call_when_not_logged_in_still_succeeds_without_a_token(self) -> None:
        captured: list[str | None] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.headers.get("authorization"))
            return httpx.Response(200, json=_STATUS_BODY_ANONYMOUS)

        client = _not_logged_in_client(_handler)

        result = client.get_status()

        assert captured == [None]
        assert result == ServiceStatus(
            status="ready", version="1.4.0", unhealthy_dependencies=None, gated_graphs=None
        )

    def test_default_call_when_the_refresh_is_rejected_still_succeeds_without_a_token(
        self,
    ) -> None:
        captured: list[str | None] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.headers.get("authorization"))
            return httpx.Response(200, json=_STATUS_BODY_ANONYMOUS)

        def _reject_refresh(call_number: int) -> httpx.Response:
            del call_number
            return httpx.Response(400, json={"error": "invalid_grant"})

        transport, _ = _build_auth_and_business_transport(
            _handler, refresh_response=_reject_refresh
        )
        store = _FakeCredentialStore()
        store.set_tokens("dev", TokenBundle(refresh_token="stale-rt", issuer=_FAKE_ISSUER))
        client = PsServiceClient(
            _SERVICE_URL, transport=transport, credential_store=store, context="dev"
        )

        result = client.get_status()

        assert captured == [None]
        assert result.unhealthy_dependencies is None
