"""Integration test: `ps-cli regulations ingest <celex>` against a real spawned
`ps-service`, real FalkorDB, and a real LLM Provider.

Marked `@pytest.mark.integration` + `@pytest.mark.falkordb_live` + `@pytest.mark.llm_live`
(all three already registered in the root `pyproject.toml` -- no config change needed).
This is the true end-to-end happy-path proof for AC-BI-002 (PLAN.md Increment 17):
`ps-service` runs as a real OS subprocess against real FalkorDB and a real LLM Provider;
`ps_cli.cli.run()` runs in this test process and drives the whole `regulations ingest`
pipeline over a real HTTP round trip -- no mocks anywhere in this test.

Vendors its own `_spawn_ps_service()` / `_wait_until_*()` helpers, adapted from
`ps-cli/tests/test_integration_regulations_list.py` (Increment 16) and from
`ps-service/tests/test_main_integration.py`'s subprocess-spawn/health-poll pattern --
copied, not imported cross-file/cross-member, per L2 Project Structure's "fully
decoupled ... vendors its own copy of anything it needs". A small amount of duplication
against Increment 16's own helpers is deliberate, not an oversight (CONTEXT.md).

Unlike Increment 16 (`GET /regulations`, a static in-memory catalog, needs only
`/health`), a real ingestion needs FalkorDB, the LLM Provider, and Cellar/ELI all
reachable -- so this test additionally polls `GET /ready` (confirmed by reading
`ps_service/main.py::ready()`: `app.state.ready` from the one-time startup dependency
probe AND the live `dependency_health.all_healthy(...)` signal both have to hold) before
issuing the ingestion call.

`.env` (repo root) is loaded explicitly via `python-dotenv` at import time -- the same
library `litellm` itself uses for its own import-time `dotenv.load_dotenv()` side effect
(see `ps-service/tests/conftest.py`'s docstring) -- so the loaded values can be threaded
explicitly into the *subprocess*'s environment (the implicit litellm-import side effect
only populates the process that triggers it, which would be this test process, not the
separately-spawned `ps_service` subprocess).

The ingestion is pointed at a disposable FalkorDB graph (`PS_FALKORDB_GRAPH`), deleted in
teardown, so this test never writes to the real shared `policy_system` graph -- mirroring
`ps-service/tests/api/test_live_capstone_external.py`'s own isolation precedent. Cleanup
connects to FalkorDB directly via the `falkordb` package (a test-only concern, not a
`ps_cli` production import -- `ps-cli/tests/test_architecture_boundary.py` only scans
`ps-cli/src/ps_cli/**`, so this is not an AC-BI-004 violation).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

import dotenv
import httpx
import pytest
from falkordb import FalkorDB  # test-only: not a ps_cli production import, see module docstring

from ps_cli.cli import run

if TYPE_CHECKING:
    from collections.abc import Iterator

if sys.platform == "win32":  # pragma: no cover - documented platform caveat, not exercised here
    pytest.skip("subprocess signal semantics differ on Windows", allow_module_level=True)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOST = "127.0.0.1"
_HEALTH_POLL_TIMEOUT_SECONDS = 10.0
# Startup's dependency probe makes one real LLM completion + one real LLM embedding call
# (`llm_interface/connectivity.py::check_connectivity`) plus FalkorDB/Cellar-ELI pings --
# generous but bounded, matching this increment's "don't invent an arbitrarily short bound"
# instruction.
_READY_POLL_TIMEOUT_SECONDS = 90.0
_POLL_INTERVAL_SECONDS = 0.2
_TERMINATE_WAIT_TIMEOUT_SECONDS = 10
_FALKORDB_HOST = "127.0.0.1"
_FALKORDB_PORT = 6379
_DISPOSABLE_GRAPH = "ps_cli_integration_test"

# CRA -- the smallest curated instrument, matching #51's own capstone precedent
# (`ps-service/tests/api/test_live_capstone_external.py`'s module docstring).
_CRA_CELEX = "32024R2847"
_EXPECTED_STAGES = ("ingestion", "extraction", "derivation", "merge")

dotenv.load_dotenv(_REPO_ROOT / ".env")

_CHAT_MODEL = os.environ.get("PS_LLMINTERFACE_MODEL")
_EMBED_MODEL = os.environ.get("PS_LLMINTERFACE_EMBED_MODEL")
_AZURE_API_KEY = os.environ.get("AZURE_API_KEY")
_AZURE_API_BASE = os.environ.get("AZURE_API_BASE")

# `ps_service.api.ingestion_orchestration` requires this to be non-None before running any
# pipeline (`IngestionConfigIncompleteError` / `ingestion_config_incomplete`, 503, otherwise)
# -- it has no built-in default. `.env` does not carry it, so it is defaulted here, matching
# `ps-service/tests/api/test_live_capstone_external.py`'s own precedent value exactly.
_DEFAULT_COMPANY_MERGE_SIMILARITY_THRESHOLD = "0.85"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.falkordb_live,
    pytest.mark.llm_live,
    pytest.mark.skipif(
        not (_CHAT_MODEL and _EMBED_MODEL and _AZURE_API_KEY and _AZURE_API_BASE),
        reason=(
            "requires .env sourced (PS_LLMINTERFACE_MODEL/_EMBED_MODEL, "
            "AZURE_API_KEY, AZURE_API_BASE)"
        ),
    ),
]


def _find_free_port() -> int:
    """Bind a socket to port 0, read the OS-assigned port, close it, return the number.

    Same accepted bind-close-reuse-port TOCTOU pattern as Increment 13/16.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind((_HOST, 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _spawn_ps_service(port: int, log_dir: Path) -> subprocess.Popen[bytes]:
    """Spawn `python -m ps_service` directly, bound to `port`, with real LLM credentials.

    `PS_FALKORDB_GRAPH` points the subprocess at a disposable graph name so this run never
    touches the real shared `policy_system` graph (see module docstring). Credentials are
    threaded explicitly from this process's already-`dotenv`-loaded `os.environ`, not left
    to the subprocess's own import-time `litellm` side effect, so the exact loaded values
    are what the subprocess gets regardless of its own working-directory/search behavior.
    """
    env = {
        **os.environ,
        "PS_SERVICE_PORT": str(port),
        "PS_LOGGING_DIR": str(log_dir),
        "PS_FALKORDB_GRAPH": _DISPOSABLE_GRAPH,
        "PS_LLMINTERFACE_MODEL": cast("str", _CHAT_MODEL),
        "PS_LLMINTERFACE_EMBED_MODEL": cast("str", _EMBED_MODEL),
        "AZURE_API_KEY": cast("str", _AZURE_API_KEY),
        "AZURE_API_BASE": cast("str", _AZURE_API_BASE),
    }
    env.setdefault(
        "PS_COMPANYMERGE_SIMILARITY_THRESHOLD", _DEFAULT_COMPANY_MERGE_SIMILARITY_THRESHOLD
    )
    return subprocess.Popen(
        [sys.executable, "-m", "ps_service"],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM the subprocess, escalating to SIGKILL if it doesn't exit promptly."""
    if proc.poll() is not None:
        return  # already exited
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=_TERMINATE_WAIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=_TERMINATE_WAIT_TIMEOUT_SECONDS)


def _wait_until_healthy(base_url: str) -> None:
    """Poll `GET /health` until it responds at all, or raise `TimeoutError`.

    A response object at all (any status) means the server is accepting connections;
    `ps_service/main.py::health()` only ever returns 200 unconditionally, so a
    non-exception response is equivalent to "healthy" (same reasoning as Increment 16).
    """
    deadline = time.monotonic() + _HEALTH_POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base_url}/health", timeout=1.0)
        except httpx.HTTPError:
            time.sleep(_POLL_INTERVAL_SECONDS)
            continue
        return
    msg = f"ps_service did not become healthy within {_HEALTH_POLL_TIMEOUT_SECONDS}s"
    raise TimeoutError(msg)


def _try_get_ready_status(base_url: str) -> str | None:
    """Attempt a single `GET /ready`, returning the parsed `status` field, or `None`.

    Factored out of `_wait_until_ready`'s polling loop so the `try`/`except` lives in a
    single call, not inline inside the loop body (ruff PERF203, same pattern as
    `ps-service/tests/test_main_integration.py::_try_get_health`).
    """
    try:
        response = httpx.get(f"{base_url}/ready", timeout=2.0)
    except httpx.HTTPError:
        return None
    body = cast("dict[str, object]", response.json())
    status = body.get("status")
    return status if isinstance(status, str) else None


def _wait_until_ready(base_url: str) -> None:
    """Poll `GET /ready` until it reports `{"status": "ready"}`, or raise `TimeoutError`.

    Confirmed by reading `ps_service/main.py::ready()`: `is_ready` requires BOTH the
    one-time startup dependency probe (`app.state.ready`, itself gated on FalkorDB/LLM
    Interface/Cellar-ELI all succeeding once at startup) AND the live
    `dependency_health.all_healthy(...)` signal -- polling is needed because the startup
    probe runs asynchronously in `lifespan`, not synchronously before `/health` starts
    responding.
    """
    deadline = time.monotonic() + _READY_POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _try_get_ready_status(base_url) == "ready":
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
    msg = f"ps_service did not become ready within {_READY_POLL_TIMEOUT_SECONDS}s"
    raise TimeoutError(msg)


def _delete_disposable_graph_if_exists() -> None:
    """Best-effort drop of the disposable graph this test ingested into, from real FalkorDB."""
    db = FalkorDB(host=_FALKORDB_HOST, port=_FALKORDB_PORT)
    if _DISPOSABLE_GRAPH in db.list_graphs():
        db.select_graph(_DISPOSABLE_GRAPH).delete()


@pytest.fixture
def running_ps_service(tmp_path: Path) -> Iterator[str]:
    """Spawn a real `ps_service` subprocess wired to real FalkorDB/LLM; yield its base URL.

    Polls `/health` then `/ready` before yielding, so the test body only ever issues the
    ingestion call once the pipeline's real dependencies are confirmed reachable. Tears
    down the subprocess (SIGTERM, escalating to SIGKILL) and the disposable graph in a
    `finally` block, so both are cleaned up even if the test body -- or either poll --
    raises.
    """
    port = _find_free_port()
    log_dir = tmp_path / "ps-service-logs"
    log_dir.mkdir()
    proc = _spawn_ps_service(port, log_dir)
    base_url = f"http://{_HOST}:{port}"
    try:
        _wait_until_healthy(base_url)
        _wait_until_ready(base_url)
        yield base_url
    finally:
        _terminate(proc)
        _delete_disposable_graph_if_exists()


def test_regulations_ingest_against_real_spawned_ps_service(
    running_ps_service: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-002 true happy path: `regulations ingest <celex>` end to end, no mocks.

    Only `ps-service` runs as a real OS subprocess; `ps_cli.cli.run()` runs in this test
    process and makes one real HTTP round trip against it over `PS_CLI_SERVICE_URL`. PS
    Service's own pipeline underneath that one HTTP call drives real FalkorDB writes and
    real LLM Provider calls (Cellar/ELI fetch -> Domain Mapper extraction -> derivation ->
    Company Merge). Asserts exit code 0, a `run_id` and `regulatory_instrument_id` are
    printed (AC-BI-010), and every one of the four pipeline stages is reported succeeded
    (AC-BI-002) -- the same stage sequence `ps-service`'s own capstone test
    (`test_live_capstone_external.py`) observes for an unmodified real run.

    Known flake source, not a ps-cli concern: a failure here surfacing
    `DomainMapperDerivationError` at the `derivation` stage is `ps-service`'s own tracked
    LLM non-determinism, issue #45 ("live capstones flake on LLM non-determinism after a
    clean reseed, hallucinated match-id, semantic-dedup miss") -- observed once during this
    increment's own verification (2026-08-30), followed by a clean pass on immediate retry
    with no code change. If this test fails with that error, retry before treating it as a
    ps-cli regression; ps-cli's own behavior in that case (correctly surfacing the failing
    stage + reason + non-zero exit) is exactly AC-BI-008's contract working as intended, not
    a bug -- see `test_http_client.py::TestIngestCatalog::
    test_502_pipeline_stage_failed_surfaces_failing_stage` for the same path exercised
    against a synthetic mock instead.
    """
    monkeypatch.setenv("PS_CLI_SERVICE_URL", running_ps_service)

    exit_code = run(["regulations", "ingest", _CRA_CELEX], client=None)

    captured = capsys.readouterr()
    assert exit_code == 0, f"stdout={captured.out!r} stderr={captured.err!r}"
    assert "run_id:" in captured.out
    assert "regulatory_instrument_id:" in captured.out
    for stage in _EXPECTED_STAGES:
        assert f"{stage}: succeeded" in captured.out, captured.out
