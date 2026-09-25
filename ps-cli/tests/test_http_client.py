"""Tests for ps_cli.http_client: PsServiceClient construction, AC-BI-009 warning,
and its various endpoint methods.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from ps_cli.catalog_repo import CuratedArtifact, CuratedInstrumentManifest
from ps_cli.credentials import TokenBundle
from ps_cli.errors import PsCliError
from ps_cli.http_client import (
    _UNEXPECTED_RESPONSE_SHAPE_MSG,  # pyright: ignore[reportPrivateUsage]  # asserted verbatim, per check_health()'s existing precedent
    PsServiceClient,
    _should_warn_insecure,  # pyright: ignore[reportPrivateUsage]  # PLAN.md Inc. 7: unit-tested directly per its own AC
)
from ps_cli.models import ReadinessResult

if TYPE_CHECKING:
    from collections.abc import Callable

# Shared wire-contract literal (issue #91, CHANGES.md A2): both ps-service's and
# ps-cli's own test suites read the internal-ingestion envelope's field name from
# this one file (canonically owned by ps-service, since it defines the internal
# envelope contract), so a one-sided rename of `content` breaks the *other* side's
# test rather than going unnoticed by either.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WIRE_CONTRACTS_DIR = _REPO_ROOT / "ps-service" / "tests" / "fixtures" / "wire-contracts"
_ENVELOPE_CONTRACT_PATH = _WIRE_CONTRACTS_DIR / "ingest-internal-envelope.json"
_ENVELOPE_CONTRACT: dict[str, object] = json.loads(_ENVELOPE_CONTRACT_PATH.read_text())


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


class TestIngestInternal:
    """Increment 14: PsServiceClient.ingest_internal(content)."""

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
    """Issue #71, new S3 (CHANGES.md A2): PsServiceClient.export_instrument(instrument_id)."""

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

    def test_404_export_instrument_not_found_raises_ps_cli_error(self) -> None:
        """A 404 export_instrument_not_found body maps to PsCliError.

        Exercises the *existing*, unmodified `_raise_from_error_body` (D7) -- proves
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
        """A 503 export_config_incomplete body maps to PsCliError per D7's mapping."""
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
        """A transport-level ConnectError maps to PsCliError per D5's mapping."""
        client = PsServiceClient(
            "http://127.0.0.1:8000", transport=httpx.MockTransport(_connect_error_handler)
        )

        with pytest.raises(PsCliError) as excinfo:
            client.export_instrument("CRA-1.0")

        assert "Could not reach PS Service" in excinfo.value.msg

    def test_read_timeout_raises_ps_cli_error(self) -> None:
        """A transport-level ReadTimeout maps to PsCliError per D5's mapping."""

        def _handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        client = PsServiceClient("http://127.0.0.1:8000", transport=httpx.MockTransport(_handler))

        with pytest.raises(PsCliError) as excinfo:
            client.export_instrument("CRA-1.0")

        assert "did not respond in time" in excinfo.value.msg

    def test_posts_with_the_extended_export_read_timeout(self) -> None:
        """D8: export_instrument() passes the extended 1800s read timeout, not restore's 300s.

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


def _health_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/health"
    assert request.method == "GET"
    return httpx.Response(200, json={"status": "alive"})


class TestCheckHealth:
    """Slice 4: PsServiceClient.check_health()."""

    def test_check_health_returns_status_string_on_success(self) -> None:
        """A 200 GET /health body's `status` field is returned as-is."""
        client = PsServiceClient(
            "http://127.0.0.1:8000", transport=httpx.MockTransport(_health_handler)
        )

        status = client.check_health()

        assert status == "alive"

    def test_check_health_raises_ps_cli_error_on_connect_failure(self) -> None:
        """A transport-level ConnectError maps to PsCliError per D5/D6's mapping."""
        client = PsServiceClient(
            "http://127.0.0.1:8000", transport=httpx.MockTransport(_connect_error_handler)
        )

        with pytest.raises(PsCliError) as excinfo:
            client.check_health()

        assert "Could not reach PS Service at" in excinfo.value.msg
        assert "http://127.0.0.1:8000" in excinfo.value.msg
        assert excinfo.value.hint is not None
        assert "PS_CLI_SERVICE_URL" in excinfo.value.hint

    @pytest.mark.parametrize(
        "body",
        [
            ["not", "a", "dict"],
            {"status": 123},
            {"no_status_key": "alive"},
        ],
    )
    def test_check_health_raises_generic_error_on_malformed_body(self, body: object) -> None:
        """A malformed /health body raises the generic PsCliError, not a new exception type."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
        )

        with pytest.raises(PsCliError) as excinfo:
            client.check_health()

        assert excinfo.value.msg == _UNEXPECTED_RESPONSE_SHAPE_MSG


def _service_version_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/health"
    assert request.method == "GET"
    return httpx.Response(200, json={"status": "alive", "version": "1.4.0"})


class TestGetServiceVersion:
    """Slice 2 (issue #82): PsServiceClient.get_service_version()."""

    def test_get_service_version_returns_version_string_on_success(self) -> None:
        """A 200 GET /health body's `version` field is returned as-is."""
        client = PsServiceClient(
            "http://127.0.0.1:8000", transport=httpx.MockTransport(_service_version_handler)
        )

        version = client.get_service_version()

        assert version == "1.4.0"

    def test_get_service_version_raises_ps_cli_error_on_connect_failure(self) -> None:
        """A transport-level ConnectError maps to PsCliError per D5/D6's mapping."""
        client = PsServiceClient(
            "http://127.0.0.1:8000", transport=httpx.MockTransport(_connect_error_handler)
        )

        with pytest.raises(PsCliError) as excinfo:
            client.get_service_version()

        assert "Could not reach PS Service at" in excinfo.value.msg
        assert "http://127.0.0.1:8000" in excinfo.value.msg
        assert excinfo.value.hint is not None
        assert "PS_CLI_SERVICE_URL" in excinfo.value.hint

    def test_get_service_version_raises_ps_cli_error_on_connection_reset(self) -> None:
        """A transport-level ReadError (e.g. connection reset) maps to PsCliError too.

        Not just ConnectError/ConnectTimeout/ReadTimeout. Regression test: this used to
        propagate as a raw httpx.ReadError, crashing `ps-cli --version` with a traceback
        instead of the documented "unavailable" line, even though AC-BI-008 requires
        `--version` to exit 0 whenever PS Service is unreachable.
        """
        client = PsServiceClient(
            "http://127.0.0.1:8000", transport=httpx.MockTransport(_read_error_handler)
        )

        with pytest.raises(PsCliError) as excinfo:
            client.get_service_version()

        assert "Could not reach PS Service at" in excinfo.value.msg
        assert "http://127.0.0.1:8000" in excinfo.value.msg
        assert excinfo.value.hint is not None
        assert "PS_CLI_SERVICE_URL" in excinfo.value.hint

    @pytest.mark.parametrize(
        "body",
        [
            ["not", "a", "dict"],
            {"status": "alive"},
            {"version": 123},
        ],
    )
    def test_get_service_version_raises_generic_error_on_malformed_body(self, body: object) -> None:
        """A malformed /health body raises the generic PsCliError, not a new exception type."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
        )

        with pytest.raises(PsCliError) as excinfo:
            client.get_service_version()

        assert excinfo.value.msg == _UNEXPECTED_RESPONSE_SHAPE_MSG


_READY_BODY_HEALTHY: dict[str, object] = {"status": "ready", "unhealthy_dependencies": []}
_READY_BODY_NOT_READY: dict[str, object] = {
    "status": "not_ready",
    "unhealthy_dependencies": ["falkordb", "cellar_eli"],
}


def _make_ready_handler(body: object) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ready"
        assert request.method == "GET"
        return httpx.Response(200, json=body)

    return _handler


class TestCheckReadiness:
    """Slice 5: PsServiceClient.check_readiness()."""

    def test_check_readiness_returns_readiness_result_when_ready(self) -> None:
        """A ready /ready body parses into a ReadinessResult with an empty list."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(_make_ready_handler(_READY_BODY_HEALTHY)),
        )

        result = client.check_readiness()

        assert result == ReadinessResult(status="ready", unhealthy_dependencies=[])

    def test_check_readiness_returns_readiness_result_when_not_ready_with_unhealthy_names(
        self,
    ) -> None:
        """A not-ready /ready body's unhealthy_dependencies names surface verbatim."""
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(_make_ready_handler(_READY_BODY_NOT_READY)),
        )

        result = client.check_readiness()

        assert result == ReadinessResult(
            status="not_ready", unhealthy_dependencies=["falkordb", "cellar_eli"]
        )

    def test_check_readiness_raises_ps_cli_error_on_connect_failure(self) -> None:
        """A transport-level ConnectError maps to PsCliError per D5/D6's mapping."""
        client = PsServiceClient(
            "http://127.0.0.1:8000", transport=httpx.MockTransport(_connect_error_handler)
        )

        with pytest.raises(PsCliError) as excinfo:
            client.check_readiness()

        assert "Could not reach PS Service at" in excinfo.value.msg
        assert excinfo.value.hint is not None
        assert "PS_CLI_SERVICE_URL" in excinfo.value.hint

    @pytest.mark.parametrize(
        "body",
        [
            ["not", "a", "dict"],
            {"status": 123, "unhealthy_dependencies": []},
            {"status": "ready"},
            {"status": "ready", "unhealthy_dependencies": "not-a-list"},
            {"status": "ready", "unhealthy_dependencies": ["falkordb", 123]},
        ],
    )
    def test_check_readiness_raises_generic_error_on_malformed_body(self, body: object) -> None:
        """Every malformed /ready body shape raises the generic PsCliError.

        Covers: non-dict body, status not a string, missing unhealthy_dependencies
        key, unhealthy_dependencies not a list, and a non-string list item.
        """
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
        )

        with pytest.raises(PsCliError) as excinfo:
            client.check_readiness()

        assert excinfo.value.msg == _UNEXPECTED_RESPONSE_SHAPE_MSG


# --- Issue #121 Group 3 (AC-BI-002/003/004/005/006/009): token use, refresh-once ----
#
# `PsServiceClient` threads its own `transport` constructor argument into
# `device_flow.ensure_valid_access_token` too (issue #121: `self._transport`), so one
# `httpx.MockTransport` can answer PS Service's resource-metadata endpoint, the fake
# issuer's openid-configuration and token endpoints, *and* PS Service's own business
# endpoint -- a genuine wire-level fake, not a `resolve_auth_parameters` monkeypatch,
# per CHANGES.md MINOR-2's "fake-transport, refresh-call-counting" design.

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
) -> tuple[httpx.MockTransport, list[int]]:
    """One fake transport answering resource-metadata/discovery/refresh + business calls.

    `refresh_response`, when given, is called with the 1-based call number on every
    `POST <issuer>/token` and its return value is sent back verbatim -- lets a test
    simulate a refresh rejection. Defaults to minting `refreshed-token-<n>` on success.
    Returns `(transport, call_count)` where `call_count[0]` is mutated on every refresh
    call, so a test can assert exactly how many refreshes happened.
    """
    call_count = [0]

    def _handle(request: httpx.Request) -> httpx.Response:
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
    """Issue #121 (AC-BI-003/004): exactly one refresh per invocation, then reused."""

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
        """AC-BI-003/004's core proof: a second authenticated call on the *same*
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

    def test_check_health_attaches_no_authorization_header_even_with_credential_store_given(
        self,
    ) -> None:
        """D-57-6: `check_health` is exempt -- never attaches a header, never refreshes,
        even given credentials.
        """
        captured_headers: list[str | None] = []

        def _business_handler(request: httpx.Request) -> httpx.Response:
            captured_headers.append(request.headers.get("authorization"))
            return httpx.Response(200, json={"status": "alive"})

        transport, refresh_calls = _build_auth_and_business_transport(_business_handler)
        store = _FakeCredentialStore()
        store.set_tokens("dev", TokenBundle(refresh_token="seed-rt", issuer=_FAKE_ISSUER))
        client = PsServiceClient(
            "http://127.0.0.1:8000",
            transport=transport,
            credential_store=store,
            context="dev",
        )

        client.check_health()

        assert captured_headers == [None]
        assert refresh_calls == [0]

    def test_construction_with_no_auth_params_is_byte_for_byte_unaffected(self) -> None:
        """The pre-#57 construction shapes still work: no header is ever attached.

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
    """Issue #121 (AC-BI-002/009): no-refresh-token / refresh-fails fail closed."""

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
    """Issue #57 Slice 18 (AC-BI-014): a 401 maps to one actionable error, never the body."""

    def test_ingest_internal_maps_401_response_to_actionable_error_not_generic_http_error(
        self,
    ) -> None:
        """A bare 401, with PS Service's own structured error body attached, still maps
        to exactly the AC-BI-014 wording -- the body's own `code`/`message` never leak
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
        """Issue #57 Slice 22 (AC-BI-018): a marker access token, already attached as
        the request's own bearer header when the 401 comes back, never leaks into
        `.msg`/`.hint` -- a proof pass over Slice 18's actual code, not a new
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
