"""Tests for ps_cli.http_client: PsServiceClient construction, AC-BI-009 warning,
and list_regulations().
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

import httpx
import pytest

from ps_cli.catalog_repo import CuratedArtifact, CuratedInstrumentManifest
from ps_cli.errors import PsCliError
from ps_cli.http_client import (
    PsServiceClient,
    _should_warn_insecure,  # pyright: ignore[reportPrivateUsage]  # PLAN.md Inc. 7: unit-tested directly per its own AC
)

if TYPE_CHECKING:
    from collections.abc import Callable


class TestShouldWarnInsecure:
    """Table-driven proof of the AC-BI-009 heuristic (PLAN.md §1 D4)."""

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
    """AC-BI-009: the insecure-URL warning is printed to stderr, once, at construction."""

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


_CATALOG_BODY = {
    "regulations": [
        {"celex": "32016R0679", "title": "General Data Protection Regulation"},
        {"celex": "32019R0881", "title": "Cybersecurity Act"},
    ],
    "run_id": "run-abc123",
}


def _unused_handler(request: httpx.Request) -> httpx.Response:
    """A transport handler that should never be invoked (construction-only tests)."""
    msg = f"unexpected request in a construction-only test: {request.url}"
    raise AssertionError(msg)


def _catalog_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/regulations"
    return httpx.Response(200, json=_CATALOG_BODY)


def _connect_error_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


class TestListRegulations:
    """Increment 8: PsServiceClient.list_regulations()."""

    def test_parses_a_200_catalog_response(self) -> None:
        """A 200 GET /regulations body parses into RegulationEntry list + run_id."""
        client = PsServiceClient(
            "http://127.0.0.1:8000", transport=httpx.MockTransport(_catalog_handler)
        )

        result = client.list_regulations()

        assert result.run_id == "run-abc123"
        assert len(result.regulations) == 2
        assert result.regulations[0].celex == "32016R0679"
        assert result.regulations[0].title == "General Data Protection Regulation"
        assert result.regulations[1].celex == "32019R0881"
        assert result.regulations[1].title == "Cybersecurity Act"

    def test_connect_error_raises_ps_cli_error_with_actionable_message(self) -> None:
        """A transport-level ConnectError maps to PsCliError per D5's mapping."""
        client = PsServiceClient(
            "http://127.0.0.1:8000", transport=httpx.MockTransport(_connect_error_handler)
        )

        with pytest.raises(PsCliError) as excinfo:
            client.list_regulations()

        assert "Could not reach PS Service at" in excinfo.value.msg
        assert "http://127.0.0.1:8000" in excinfo.value.msg
        assert excinfo.value.hint is not None
        assert "PS_CLI_SERVICE_URL" in excinfo.value.hint

    def test_connect_timeout_raises_ps_cli_error_with_actionable_message(self) -> None:
        """A transport-level ConnectTimeout also maps to the same actionable PsCliError."""

        def _timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        client = PsServiceClient(
            "http://127.0.0.1:8000", transport=httpx.MockTransport(_timeout_handler)
        )

        with pytest.raises(PsCliError) as excinfo:
            client.list_regulations()

        assert "Could not reach PS Service at" in excinfo.value.msg

    def test_read_timeout_raises_ps_cli_error_with_actionable_message(self) -> None:
        """A transport-level ReadTimeout maps to an actionable PsCliError per D5's mapping.

        Found via the Increment 17 live run against a real ingestion pipeline: PS Service
        legitimately does not respond within the client's connect window on a slow real
        request, and this must surface as a clean `PsCliError`, not an uncaught
        `httpx.ReadTimeout` bug.
        """

        def _read_timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        client = PsServiceClient(
            "http://127.0.0.1:8000", transport=httpx.MockTransport(_read_timeout_handler)
        )

        with pytest.raises(PsCliError) as excinfo:
            client.list_regulations()

        assert "did not respond in time" in excinfo.value.msg
        assert "http://127.0.0.1:8000" in excinfo.value.msg

    def test_uses_the_unmodified_client_wide_timeout(self) -> None:
        """Regression guard (OPEN_QUESTIONS_RESOLVED.md item 10 / BATCH_H_FIX.md).

        `GET /regulations` is fast and static -- it must keep inheriting the
        client-wide 5s connect / 30s read timeout unchanged, never the extended
        30-minute read timeout that `POST /ingestions` now applies per-request. This
        pins that boundary so a future edit can't silently widen `list_regulations()`'s
        timeout too.
        """
        captured_timeouts: list[object] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured_timeouts.append(request.extensions.get("timeout"))
            return httpx.Response(200, json=_CATALOG_BODY)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        client.list_regulations()

        assert captured_timeouts == [{"connect": 5.0, "read": 30.0, "write": 5.0, "pool": 5.0}]


_INGESTION_SUCCESS_BODY = {
    "run_id": "run-ingest-001",
    "regulatory_instrument_id": "ri-gdpr",
    "source": "catalog",
    "stages": [
        {"stage": "parse", "status": "succeeded", "summary": {"nodes": 3}},
        {"stage": "merge", "status": "succeeded", "summary": {"edges": 5}},
    ],
}


def _make_ingestion_success_handler() -> Callable[[httpx.Request], httpx.Response]:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ingestions"
        assert request.method == "POST"
        return httpx.Response(200, json=_INGESTION_SUCCESS_BODY)

    return _handler


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


class TestIngestCatalog:
    """Increment 11: PsServiceClient.ingest_catalog(celex)."""

    def test_parses_a_200_success_response(self) -> None:
        """A 200 POST /ingestions body parses into an IngestionResult."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(_make_ingestion_success_handler()),
        )

        result = client.ingest_catalog("32016R0679")

        assert result.run_id == "run-ingest-001"
        assert result.regulatory_instrument_id == "ri-gdpr"
        assert result.source == "catalog"
        assert len(result.stages) == 2
        assert result.stages[0].stage == "parse"
        assert result.stages[0].status == "succeeded"
        assert result.stages[0].summary == {"nodes": 3}
        assert result.stages[1].stage == "merge"
        assert result.stages[1].summary == {"edges": 5}

    def test_posts_the_expected_request_body(self) -> None:
        """The request body is {"source": "catalog", "celex": celex}."""
        captured_bodies: list[object] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(json.loads(request.content))
            return httpx.Response(200, json=_INGESTION_SUCCESS_BODY)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        client.ingest_catalog("32016R0679")

        assert captured_bodies == [{"source": "catalog", "celex": "32016R0679"}]

    def test_404_catalog_identifier_not_found_raises_ps_cli_error(self) -> None:
        """A 404 catalog_identifier_not_found body maps to PsCliError per D5."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(
                _make_error_body_handler(
                    status_code=404,
                    code="catalog_identifier_not_found",
                    message="CELEX '39999X9999' is not in the curated catalog.",
                )
            ),
        )

        with pytest.raises(PsCliError) as excinfo:
            client.ingest_catalog("39999X9999")

        assert "catalog_identifier_not_found" in excinfo.value.msg
        assert "not in the curated catalog" in excinfo.value.msg

    def test_502_pipeline_stage_failed_surfaces_failing_stage(self) -> None:
        """A 502 pipeline_stage_failed body's failing_stage surfaces in the raised error."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(
                _make_error_body_handler(
                    status_code=502,
                    code="pipeline_stage_failed",
                    message="the domain mapper stage failed",
                    failing_stage="domain_mapper",
                )
            ),
        )

        with pytest.raises(PsCliError) as excinfo:
            client.ingest_catalog("32016R0679")

        assert "pipeline_stage_failed" in excinfo.value.msg
        assert "domain_mapper" in excinfo.value.msg

    def test_503_ingestion_config_incomplete_raises_ps_cli_error(self) -> None:
        """A 503 ingestion_config_incomplete body maps to PsCliError per D5."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(
                _make_error_body_handler(
                    status_code=503,
                    code="ingestion_config_incomplete",
                    message="FalkorDB configuration is incomplete.",
                )
            ),
        )

        with pytest.raises(PsCliError) as excinfo:
            client.ingest_catalog("32016R0679")

        assert "ingestion_config_incomplete" in excinfo.value.msg

    def test_500_internal_error_raises_generic_ps_cli_error(self) -> None:
        """A 500 internal_error catch-all body maps to PsCliError per D5."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(
                _make_error_body_handler(
                    status_code=500,
                    code="internal_error",
                    message="An internal error occurred.",
                )
            ),
        )

        with pytest.raises(PsCliError) as excinfo:
            client.ingest_catalog("32016R0679")

        assert "internal_error" in excinfo.value.msg

    def test_non_json_502_body_falls_back_to_generic_error_without_crashing(self) -> None:
        """A malformed (non-JSON) error body falls back to a generic PsCliError -- no crash."""

        def _handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(502, content=b"<html>not json</html>")

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        with pytest.raises(PsCliError) as excinfo:
            client.ingest_catalog("32016R0679")

        assert "unexpected error response" in excinfo.value.msg
        assert "502" in excinfo.value.msg

    def test_posts_with_the_extended_ingestion_read_timeout(self) -> None:
        """OPEN_QUESTIONS_RESOLVED.md item 10 / BATCH_H_FIX.md.

        `POST /ingestions` blocks synchronously for the whole real pipeline (a real CRA
        ingestion measured 612.86s / 10m12s) -- the client-wide 30s read timeout is far
        too short for it. This proves `ingest_catalog()` passes a per-request override
        widening only the read timeout to 1800s (30 min); connect/write/pool stay at the
        fast client-wide 5s, since a slow *response* is expected here but a slow
        *connection* is not.
        """
        captured_timeouts: list[object] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured_timeouts.append(request.extensions.get("timeout"))
            return httpx.Response(200, json=_INGESTION_SUCCESS_BODY)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        client.ingest_catalog("32016R0679")

        assert captured_timeouts == [{"connect": 5.0, "read": 1800.0, "write": 5.0, "pool": 5.0}]


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


class TestIngestInternal:
    """Increment 14: PsServiceClient.ingest_internal(fixture_path)."""

    def test_parses_a_200_success_response(self) -> None:
        """A 200 POST /ingestions body with source "internal" parses into an IngestionResult."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(_internal_ingestion_success_handler),
        )

        result = client.ingest_internal("seeds/internal-sop.json")

        assert result.run_id == "run-internal-001"
        assert result.regulatory_instrument_id == "ri-internal"
        assert result.source == "internal"
        assert len(result.stages) == 1
        assert result.stages[0].stage == "parse"
        assert result.stages[0].status == "succeeded"
        assert result.stages[0].summary == {"nodes": 1}

    def test_posts_the_expected_request_body(self) -> None:
        """The request body is {"source": "internal", "fixture_path": fixture_path}."""
        captured_bodies: list[object] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(json.loads(request.content))
            return httpx.Response(200, json=_INTERNAL_INGESTION_SUCCESS_BODY)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        client.ingest_internal("seeds/internal-sop.json")

        assert captured_bodies == [
            {"source": "internal", "fixture_path": "seeds/internal-sop.json"}
        ]

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
            client.ingest_internal("seeds/internal-sop.json")

        assert "internal_ingestion_not_implemented" in excinfo.value.msg
        assert _INTERNAL_NOT_IMPLEMENTED_MESSAGE in excinfo.value.msg
        assert excinfo.value.hint is not None
        assert "run-internal-501" in excinfo.value.hint

    def test_posts_with_the_extended_ingestion_read_timeout(self) -> None:
        """OPEN_QUESTIONS_RESOLVED.md item 10 / BATCH_H_FIX.md.

        Same per-request timeout override as `ingest_catalog()` (both endpoints share the
        same real pipeline behind `POST /ingestions`) -- proves `ingest_internal()` also
        passes the extended 1800s read timeout, not the client-wide 30s default.
        """
        captured_timeouts: list[object] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured_timeouts.append(request.extensions.get("timeout"))
            return httpx.Response(200, json=_INTERNAL_INGESTION_SUCCESS_BODY)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        client.ingest_internal("seeds/internal-sop.json")

        assert captured_timeouts == [{"connect": 5.0, "read": 1800.0, "write": 5.0, "pool": 5.0}]


class TestIngestCatalogRunId:
    """Increment 14: `ingest_catalog(celex, *, run_id=...)`'s request-body shape."""

    def test_includes_run_id_in_request_body_when_given(self) -> None:
        """`run_id` is included in the POST body when the caller supplies one."""
        captured_bodies: list[object] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(json.loads(request.content))
            return httpx.Response(200, json=_INGESTION_SUCCESS_BODY)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        client.ingest_catalog("32016R0679", run_id="run-explicit-123")

        assert captured_bodies == [
            {"source": "catalog", "celex": "32016R0679", "run_id": "run-explicit-123"}
        ]

    def test_omits_run_id_from_request_body_when_not_given(self) -> None:
        """Regression: today's exact wire shape is preserved when `run_id` is not passed."""
        captured_bodies: list[object] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(json.loads(request.content))
            return httpx.Response(200, json=_INGESTION_SUCCESS_BODY)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        client.ingest_catalog("32016R0679")

        assert captured_bodies == [{"source": "catalog", "celex": "32016R0679"}]


_STATUS_BODY_WITH_STAGE = {"run_id": "run-poll-001", "stage": "extraction"}
_STATUS_BODY_NO_STAGE = {"run_id": "run-poll-001", "stage": None}


class TestPollIngestionStatus:
    """Increment 14: PsServiceClient.poll_ingestion_status(run_id)."""

    def test_parses_the_stage_field(self) -> None:
        """A 200 GET /ingestions/{run_id} body's `stage` field is returned as-is."""

        def _handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/ingestions/run-poll-001"
            assert request.method == "GET"
            return httpx.Response(200, json=_STATUS_BODY_WITH_STAGE)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        stage = client.poll_ingestion_status("run-poll-001")

        assert stage == "extraction"

    def test_returns_none_when_stage_field_is_null(self) -> None:
        """A 200 body with `stage: null` (no run in flight) returns None, not a crash."""

        def _handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, json=_STATUS_BODY_NO_STAGE)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        stage = client.poll_ingestion_status("run-poll-001")

        assert stage is None

    def test_returns_none_on_network_error_without_raising(self) -> None:
        """A transport-level ConnectError is swallowed -- this is a best-effort read."""

        def _handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        stage = client.poll_ingestion_status("run-poll-001")

        assert stage is None

    def test_returns_none_on_read_timeout_without_raising(self) -> None:
        """A transport-level ReadTimeout is also swallowed -- never surfaced to the caller."""

        def _handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        stage = client.poll_ingestion_status("run-poll-001")

        assert stage is None

    def test_returns_none_on_non_2xx_response_without_raising(self) -> None:
        """A non-2xx response (e.g. a 404/500) is swallowed, not mapped to PsCliError."""

        def _handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(500, json={"error": "boom"})

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        stage = client.poll_ingestion_status("run-poll-001")

        assert stage is None

    def test_returns_none_on_non_json_body_without_raising(self) -> None:
        """A malformed (non-JSON) body is swallowed, not raised."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"<html>not json</html>")
            ),
        )

        stage = client.poll_ingestion_status("run-poll-001")

        assert stage is None

    def test_returns_none_on_wrong_shaped_body_without_raising(self) -> None:
        """A 200 body that parses as JSON but doesn't match the expected shape is swallowed."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"unexpected": "shape"})
            ),
        )

        stage = client.poll_ingestion_status("run-poll-001")

        assert stage is None

    def test_uses_a_short_timeout_not_the_1800s_override(self) -> None:
        """A recording transport proves the new short timeout constant is used, not the
        1800s `_INGESTION_REQUEST_TIMEOUT` override sized for `POST /ingestions`.
        """
        captured_timeouts: list[object] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured_timeouts.append(request.extensions.get("timeout"))
            return httpx.Response(200, json=_STATUS_BODY_WITH_STAGE)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        client.poll_ingestion_status("run-poll-001")

        assert captured_timeouts == [{"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}]


_RESTORATION_MANIFEST = CuratedInstrumentManifest(
    instrument_id="CRA-1.0",
    celex="32024R2847",
    title="Cyber Resilience Act",
    short_name="CRA",
    version="1.0",
    source_type="external",
    jurisdiction="EU",
    schema_version="1.0.0",
    exported_at="2026-09-04T00:00:00Z",
    baseline_sha256="a" * 64,
    native_sha256="b" * 64,
)

_RESTORATION_ARTIFACT = CuratedArtifact(
    manifest=_RESTORATION_MANIFEST,
    baseline_blob=b'{"nodes": [], "edges": []}',
    native_blob=b'{"nodes": [], "edges": []}',
)

_RESTORATION_SUCCESS_BODY = {
    "instrument_id": "CRA-1.0",
    "stages": [
        {"stage": "verified", "status": "succeeded"},
        {"stage": "staged", "status": "succeeded"},
        {"stage": "merged_and_finalized", "status": "succeeded"},
    ],
}


def _restoration_success_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/restorations"
    assert request.method == "POST"
    return httpx.Response(200, json=_RESTORATION_SUCCESS_BODY)


class TestRestoreInstrument:
    """Slice 7.2: PsServiceClient.restore_instrument(artifact)."""

    def test_parses_a_200_success_response(self) -> None:
        """A 200 POST /restorations body parses into a RestorationResult."""
        client = PsServiceClient(
            "http://127.0.0.1:8000", transport=httpx.MockTransport(_restoration_success_handler)
        )

        result = client.restore_instrument(_RESTORATION_ARTIFACT)

        assert result.instrument_id == "CRA-1.0"
        assert len(result.stages) == 3
        assert result.stages[0].stage == "verified"
        assert result.stages[0].status == "succeeded"
        assert result.stages[2].stage == "merged_and_finalized"

    def test_posts_the_manifest_fields_and_base64_blobs(self) -> None:
        """The request body carries instrument_id, every manifest field, and base64 blobs."""
        captured_bodies: list[object] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(json.loads(request.content))
            return httpx.Response(200, json=_RESTORATION_SUCCESS_BODY)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        client.restore_instrument(_RESTORATION_ARTIFACT)

        assert captured_bodies == [
            {
                "instrument_id": "CRA-1.0",
                "manifest": {
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
                },
                "baseline_blob_base64": base64.b64encode(b'{"nodes": [], "edges": []}').decode(
                    "ascii"
                ),
                "native_blob_base64": base64.b64encode(b'{"nodes": [], "edges": []}').decode(
                    "ascii"
                ),
            }
        ]

    def test_422_restore_artifact_rejected_raises_ps_cli_error(self) -> None:
        """A 422 restore_artifact_rejected body maps to PsCliError per D5's error-body mapping."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(
                _make_error_body_handler(
                    status_code=422,
                    code="restore_artifact_rejected",
                    message="baseline blob checksum mismatch for instrument 'CRA-1.0'",
                )
            ),
        )

        with pytest.raises(PsCliError) as excinfo:
            client.restore_instrument(_RESTORATION_ARTIFACT)

        assert "restore_artifact_rejected" in excinfo.value.msg
        assert "checksum mismatch" in excinfo.value.msg

    def test_502_restore_stage_failed_surfaces_failing_stage(self) -> None:
        """A 502 restore_stage_failed body's failing_stage surfaces in the raised error."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(
                _make_error_body_handler(
                    status_code=502,
                    code="restore_stage_failed",
                    message="the concurrency stage failed",
                    failing_stage="concurrency",
                )
            ),
        )

        with pytest.raises(PsCliError) as excinfo:
            client.restore_instrument(_RESTORATION_ARTIFACT)

        assert "restore_stage_failed" in excinfo.value.msg
        assert "concurrency" in excinfo.value.msg

    def test_connect_error_raises_ps_cli_error_with_actionable_message(self) -> None:
        """A transport-level ConnectError maps to PsCliError per D5's mapping."""
        client = PsServiceClient(
            "http://127.0.0.1:8000", transport=httpx.MockTransport(_connect_error_handler)
        )

        with pytest.raises(PsCliError) as excinfo:
            client.restore_instrument(_RESTORATION_ARTIFACT)

        assert "Could not reach PS Service" in excinfo.value.msg
