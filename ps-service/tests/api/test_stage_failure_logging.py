"""A failed pipeline stage's detail reaches the operator log, scrubbed (issue #227, AC-BI-009/010).

Drives ``POST /ingestions`` (internal) through the real ``configure()``d Logging facade with a
merge stage that raises an unlisted exception. The HTTP body stays ``"merge failed"``; the
operator-facing sinks (the JSONL file and, mirrored, stderr) carry the stage, the exception type
and a scrubbed, truncated detail.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from graph_gateway._fakes import GatewayRig, close_all_rigs

from api._fakes import build_fake_pipeline_dependencies
from api._gateway_fakes import RecordingSystemAudit, override_gateway
from ps_service.api.dependencies import get_principal, provide_pipeline_dependencies
from ps_service.api.errors import GraphWriteUnavailableError
from ps_service.api.stage_failure_log import stage_failure_detail
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


class _BoomError(Exception):
    """An exception type that is not on the safe-verbatim whitelist."""


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


@pytest.fixture(autouse=True)
def _close_rigs() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]  # autouse
    """Stop the reconciler thread of every gateway rig a test built."""
    yield
    close_all_rigs()


def _post_failing_merge(error: Exception) -> tuple[int, dict[str, object]]:
    """POST an internal ingestion whose merge stage raises ``error``; return status and body."""
    rig = GatewayRig()
    fake = build_fake_pipeline_dependencies(merge_error=error)
    app = create_app(_config())
    app.dependency_overrides[provide_pipeline_dependencies] = lambda: fake.dependencies
    app.dependency_overrides[get_principal] = lambda: Principal(sub="s", iss="https://idp")
    override_gateway(app, rig, RecordingSystemAudit(rig))
    response = TestClient(app, raise_server_exceptions=False).post(
        "/ingestions", json={"source": "internal", "content": _SEED}
    )
    return response.status_code, response.json()


def _failed_lines(text: str) -> list[dict[str, object]]:
    """Parse the ``outcome=failed`` ingestion_run JSON lines in ``text``."""
    parsed = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
    return [
        e for e in parsed if e.get("action") == "ingestion_run" and e.get("outcome") == "failed"
    ]


def test_failure_detail_is_logged_with_stage_type_and_message(mirrored_logging: Path) -> None:
    """The JSONL entry names the stage, the exception type and the message."""
    _post_failing_merge(_BoomError("graph exploded at node 42"))
    facade.reset_for_tests()  # drains the writer before reading the file

    entry = next(e for e in _failed_lines(mirrored_logging.read_text(encoding="utf-8")))
    assert entry["failing_stage"] == "merge"
    assert entry["exception_type"] == "_BoomError"
    assert "graph exploded at node 42" in str(entry["detail"])


def test_failure_detail_is_mirrored_to_stderr(
    mirrored_logging: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With mirroring on the same failed line is on stderr (what ``kubectl logs`` shows)."""
    _post_failing_merge(_BoomError("graph exploded"))
    facade.reset_for_tests()

    entry = _failed_lines(capsys.readouterr().err)[0]
    assert (entry["failing_stage"], entry["exception_type"]) == ("merge", "_BoomError")
    assert mirrored_logging.exists()


def test_secrets_urls_and_paths_are_not_logged_verbatim(
    mirrored_logging: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bearer tokens, key assignments, URLs and absolute paths are redacted in both sinks."""
    message = (
        "Authorization: Bearer abc.def.ghi api_key=sk-123456 "
        "GET https://secret.internal/graph?x=1 at /var/lib/ps/data.db"
    )
    _post_failing_merge(_BoomError(message))
    facade.reset_for_tests()

    for sink in (mirrored_logging.read_text(encoding="utf-8"), capsys.readouterr().err):
        for leaked in ("abc.def.ghi", "sk-123456", "secret.internal", "/var/lib/ps/data.db"):
            assert leaked not in sink
        assert _failed_lines(sink)


def test_detail_is_truncated_to_300_characters() -> None:
    """A 5000-character message is cut to at most 300 characters."""
    assert len(stage_failure_detail(_BoomError("x" * 5000))) <= 300


def test_response_body_is_only_the_generic_stage_message(mirrored_logging: Path) -> None:
    """The API body never carries the failure detail."""
    del mirrored_logging
    status, body = _post_failing_merge(_BoomError("secret detail"))

    assert status >= 500
    assert "secret detail" not in json.dumps(body)
    assert "merge failed" in json.dumps(body)


def test_a_whitelisted_error_logs_no_failure_detail_line(mirrored_logging: Path) -> None:
    """A safe-verbatim domain error is surfaced to the caller and not logged as stage detail."""
    _post_failing_merge(GraphWriteUnavailableError("write path unavailable"))
    facade.reset_for_tests()

    entries = _failed_lines(mirrored_logging.read_text(encoding="utf-8"))
    assert all("detail" not in e for e in entries)
