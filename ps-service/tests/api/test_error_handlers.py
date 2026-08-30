"""Tests for ``ps_service.api.error_handlers`` (AC-BI-009).

Covers the text scrubber directly, and proves that no error body this API
returns — for any ``ApiError`` subclass, a bare ``RuntimeError``, or a
``FalkorDBConnectionError`` — leaks a filesystem path, a repo/home directory, a
``host:port`` token, a URL, or a traceback.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ps_service.api.error_handlers import (
    _error_body,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly
    _scrub_text,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly
    is_safe_verbatim,
    register_exception_handlers,
)
from ps_service.api.errors import (
    CatalogIdentifierNotFoundError,
    FixturePathError,
    IngestionConfigIncompleteError,
    InternalSeedValidationError,
    PipelineStageError,
)
from ps_service.ingestion.falkordb_client import FalkorDBConnectionError

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _build_app_that_raises(exc: Exception) -> FastAPI:
    """A minimal app whose one route raises ``exc``, with the real handlers registered."""
    app = FastAPI()
    register_exception_handlers(app)

    async def _boom() -> None:
        raise exc

    app.add_api_route("/boom", _boom, methods=["GET"])
    return app


def test_scrub_text_removes_absolute_paths_repo_root_home() -> None:
    text = (
        f"failed reading {Path(__file__)} under {_REPO_ROOT}/test-data; "
        f"home was {Path.home()}/.config and also ~/.ssh/id_rsa and $HOME/x"
    )

    scrubbed = _scrub_text(text)

    assert str(_REPO_ROOT) not in scrubbed
    assert str(Path.home()) not in scrubbed
    assert "/Users/" not in scrubbed
    assert "/test-data" not in scrubbed
    assert "~/.ssh" not in scrubbed
    assert "$HOME" not in scrubbed
    assert ".py" not in scrubbed


def test_scrub_text_removes_host_port_and_urls() -> None:
    text = (
        "FalkorDB connection failed at 10.0.0.5:6379; also localhost:6379 and "
        "azure endpoint https://my-resource.openai.azure.com/v1/chat?k=secret"
    )

    scrubbed = _scrub_text(text)

    assert "10.0.0.5:6379" not in scrubbed
    assert "localhost:6379" not in scrubbed
    assert ":6379" not in scrubbed
    assert "https://" not in scrubbed
    assert "azure.com" not in scrubbed
    assert "secret" not in scrubbed


def test_error_body_has_the_fixed_shape() -> None:
    body = _error_body(code="x", message="y", run_id=None, failing_stage="merge")

    assert body == {
        "error": {"code": "x", "message": "y", "failing_stage": "merge"},
        "run_id": None,
    }


def test_is_safe_verbatim_covers_api_and_whitelisted_domain_errors() -> None:
    fake_domain_error: type[Exception] = type("DomainMapperExtractionError", (Exception,), {})

    assert is_safe_verbatim(CatalogIdentifierNotFoundError("nope"))
    assert is_safe_verbatim(IngestionConfigIncompleteError("nope"))
    assert is_safe_verbatim(fake_domain_error("boom"))  # matched by class name, no deep import
    assert not is_safe_verbatim(RuntimeError("boom"))
    assert not is_safe_verbatim(FalkorDBConnectionError("at localhost:6379"))
    assert not is_safe_verbatim(PipelineStageError(stage="merge", reason="x"))


_LEAKY_EXCEPTIONS: list[tuple[str, Exception]] = [
    (
        "catalog_not_found",
        CatalogIdentifierNotFoundError(
            f"CELEX 32099R9999 absent; searched {_REPO_ROOT}/catalog at localhost:6379",
        ),
    ),
    (
        "fixture_path",
        FixturePathError(f"{Path.home()}/secrets/evil.json escapes {_REPO_ROOT}/test-data"),
    ),
    (
        "internal_seed",
        InternalSeedValidationError(
            "seed at /tmp/x/seed.json unknown label; loaded via http://internal.host:8080/seed",
        ),
    ),
    (
        "config_incomplete",
        IngestionConfigIncompleteError("llm model unset; see /etc/ps/config.yaml"),
    ),
    (
        "pipeline_stage",
        PipelineStageError(stage="merge", reason="FalkorDB connection failed at 10.0.0.5:6379"),
    ),
    (
        "runtime_error",
        RuntimeError(f"boom at {_REPO_ROOT}/ps-service/src/ps_service/x.py, line 42"),
    ),
    (
        "falkordb",
        FalkorDBConnectionError(
            "FalkorDB connection failed at localhost:6379. Is FalkorDB running? Error: nope",
        ),
    ),
]


@pytest.mark.parametrize(
    ("label", "exc"),
    _LEAKY_EXCEPTIONS,
    ids=[label for label, _ in _LEAKY_EXCEPTIONS],
)
def test_no_error_body_contains_a_filesystem_path_or_traceback(label: str, exc: Exception) -> None:
    client = TestClient(_build_app_that_raises(exc), raise_server_exceptions=False)

    response = client.get("/boom")
    raw = response.text

    assert "Traceback" not in raw
    assert 'File "' not in raw
    assert "/Users/" not in raw
    assert str(_REPO_ROOT) not in raw
    assert str(Path.home()) not in raw
    assert ":6379" not in raw
    assert ":8080" not in raw
    assert "10.0.0.5" not in raw
    assert "http://" not in raw
    assert ".py" not in raw

    body = response.json()
    assert set(body) == {"error", "run_id"}
    assert set(body["error"]) == {"code", "message", "failing_stage"}
    assert isinstance(body["error"]["code"], str)
    assert isinstance(body["error"]["message"], str)


def test_unexpected_exception_returns_generic_500_without_detail() -> None:
    exc = RuntimeError(f"super secret detail at {Path.home()}/vault")
    client = TestClient(_build_app_that_raises(exc), raise_server_exceptions=False)

    response = client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == {
        "code": "internal_error",
        "message": "An internal error occurred.",
        "failing_stage": None,
    }
    assert "secret" not in response.text
    assert "/Users" not in response.text
    assert "run_id" in body


def test_pipeline_stage_error_names_the_failing_stage() -> None:
    exc = PipelineStageError(stage="extraction", reason="unit 7 unparseable")
    client = TestClient(_build_app_that_raises(exc), raise_server_exceptions=False)

    response = client.get("/boom")

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["failing_stage"] == "extraction"
    assert body["error"]["message"] == "unit 7 unparseable"


def test_api_errors_map_to_their_documented_status_codes() -> None:
    cases: list[tuple[Exception, int]] = [
        (CatalogIdentifierNotFoundError("x"), 404),
        (FixturePathError("x"), 400),
        (InternalSeedValidationError("x"), 422),
        (IngestionConfigIncompleteError("x"), 503),
        (PipelineStageError(stage="s", reason="r"), 502),
    ]
    for exc, expected_status in cases:
        client = TestClient(_build_app_that_raises(exc), raise_server_exceptions=False)
        assert client.get("/boom").status_code == expected_status
