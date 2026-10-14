"""An exception no handler names reaches the operator log, scrubbed.

The catch-all handler returns the generic 500 and writes one ``failed`` entry
(component ``api``, action ``unhandled_request_error``) through the real ``configure()``d
Logging facade, mirrored to stderr, carrying the request's ``run_id``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from graph_gateway._fakes import GatewayRig, close_all_rigs

from api._gateway_fakes import RecordingSystemAudit, override_gateway
from ps_service.api.dependencies import (
    get_principal,
    provide_pipeline_dependencies,
    provide_run_id,
)
from ps_service.api.error_handlers import register_exception_handlers
from ps_service.auth.models import Principal
from ps_service.config import ServiceConfig
from ps_service.logging import facade
from ps_service.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

_SEED: dict[str, object] = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "internal-sources"
        / "engineering-practices"
        / "engineering-practices-seed.json"
    ).read_text(encoding="utf-8")
)
_ACTION = "unhandled_request_error"
_GENERIC_ERROR = {
    "code": "internal_error",
    "message": "An internal error occurred.",
    "failing_stage": None,
}


class _BoomError(Exception):
    """An exception type no handler is registered for."""


@pytest.fixture(autouse=True)
def _close_rigs() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]  # autouse
    """Stop the reconciler thread of every gateway rig a test built."""
    yield
    close_all_rigs()


def _config() -> ServiceConfig:
    """A loopback config with every pipeline-required value set."""
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_model="azure/gpt-4o",
        llm_interface_embed_model="azure/text-embedding-3-large",
        company_merge_similarity_threshold=0.83,
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )


def _boom_client(message: str, *, with_run_id: bool) -> TestClient:
    """A minimal app whose ``/boom`` route raises ``_BoomError(message)``."""
    app = FastAPI()
    register_exception_handlers(app)

    async def _bound(_run_id: str = Depends(provide_run_id)) -> None:
        raise _BoomError(message)

    async def _unbound() -> None:
        raise _BoomError(message)

    app.add_api_route("/boom", _bound if with_run_id else _unbound, methods=["GET"])
    return TestClient(app, raise_server_exceptions=False)


def _entries(text: str) -> list[dict[str, object]]:
    """The ``unhandled_request_error`` JSON lines in ``text``."""
    parsed = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
    return [e for e in parsed if e.get("action") == _ACTION]


def test_unhandled_exception_is_logged_with_run_id_type_and_detail(
    mirrored_logging: Path,
) -> None:
    """One failed api entry carries the body's run_id, the exception type and the message."""
    response = _boom_client("graph exploded", with_run_id=True).get("/boom")
    facade.reset_for_tests()  # drains the writer before reading the file

    [entry] = _entries(mirrored_logging.read_text(encoding="utf-8"))
    assert (entry["component"], entry["outcome"]) == ("api", "failed")
    assert entry["exception_type"] == "_BoomError"
    assert "graph exploded" in str(entry["detail"])
    assert entry["run_id"] == response.json()["run_id"]
    assert entry["run_id"]


def test_unhandled_exception_is_mirrored_to_stderr(
    mirrored_logging: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same entry is on stderr, which is what ``kubectl logs`` shows."""
    response = _boom_client("graph exploded", with_run_id=True).get("/boom")
    facade.reset_for_tests()

    [entry] = _entries(capsys.readouterr().err)
    assert entry["run_id"] == response.json()["run_id"]
    assert mirrored_logging.exists()


def test_request_without_bound_run_id_logs_null_run_id(mirrored_logging: Path) -> None:
    """With no run_id bound the entry is still written, run_id null, response unchanged."""
    response = _boom_client("graph exploded", with_run_id=False).get("/boom")
    facade.reset_for_tests()

    [entry] = _entries(mirrored_logging.read_text(encoding="utf-8"))
    assert entry.get("run_id") is None  # a null run_id is omitted from the JSON line
    assert response.status_code == 500
    assert response.json()["error"] == _GENERIC_ERROR


def test_response_stays_generic_when_logging_is_on(mirrored_logging: Path) -> None:
    """Status, code and message are unchanged; no exception text, type or traceback leaks."""
    del mirrored_logging
    response = _boom_client("graph exploded", with_run_id=True).get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert set(body) == {"error", "run_id"}
    assert body["error"] == _GENERIC_ERROR
    for leaked in ("graph exploded", "_BoomError", "Traceback"):
        assert leaked not in response.text


def test_secrets_urls_hosts_and_paths_are_not_logged_verbatim(
    mirrored_logging: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Characterization guard: detail reuses ``stage_failure_detail`` (scrubbed, capped)."""
    message = (
        "Authorization: Bearer abc.def.ghi api_key=sk-123456 "
        "GET https://secret.internal/graph?x=1 host redis:6379 at /var/lib/ps/data.db"
    )
    _boom_client(message, with_run_id=True).get("/boom")
    facade.reset_for_tests()

    for sink in (mirrored_logging.read_text(encoding="utf-8"), capsys.readouterr().err):
        for leaked in ("abc.def.ghi", "sk-123456", "secret.internal", "6379", "data.db"):
            assert leaked not in sink
        [entry] = _entries(sink)
        detail = str(entry["detail"])
        assert detail.strip()
        assert "[redacted-secret]" in detail


def test_detail_is_capped_at_300_characters(mirrored_logging: Path) -> None:
    """A 5000-character message is logged cut to at most 300 characters."""
    _boom_client("x" * 5000, with_run_id=True).get("/boom")
    facade.reset_for_tests()

    [entry] = _entries(mirrored_logging.read_text(encoding="utf-8"))
    assert 0 < len(str(entry["detail"])) <= 300


def test_pre_stage_failure_via_real_app_is_logged_and_mirrored(
    mirrored_logging: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failure before any pipeline stage (the issue's outage) is logged under the run_id."""

    def _boom() -> None:
        raise _BoomError("graph exploded")

    rig = GatewayRig()
    app = create_app(_config())
    app.dependency_overrides[provide_pipeline_dependencies] = _boom
    app.dependency_overrides[get_principal] = lambda: Principal(sub="s", iss="https://idp")
    override_gateway(app, rig, RecordingSystemAudit(rig))
    response = TestClient(app, raise_server_exceptions=False).post(
        "/ingestions", json={"source": "internal", "content": _SEED}
    )
    facade.reset_for_tests()

    assert response.status_code == 500
    [entry] = _entries(mirrored_logging.read_text(encoding="utf-8"))
    assert entry["run_id"] == response.json()["run_id"]
    assert _entries(capsys.readouterr().err)


def test_unconfigured_emitter_still_returns_generic_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With no emitter configured the handler records a text-free fallback and returns 500."""
    facade.reset_for_tests()  # no default emitter -> the real LoggingLifecycleError path
    with caplog.at_level(logging.ERROR, logger="ps_service.api.error_handlers"):
        response = _boom_client("graph exploded", with_run_id=True).get("/boom")

    assert response.status_code == 500
    assert response.json()["error"] == _GENERIC_ERROR
    assert any(
        "could not be emitted (LoggingLifecycleError)" in r.getMessage() for r in caplog.records
    )
    assert "graph exploded" not in caplog.text
