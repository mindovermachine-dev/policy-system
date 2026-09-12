"""Integration test: `ps-cli --version` against one or more real spawned `ps-service`s.

Marked `@pytest.mark.integration` only (the marker is already registered in the root
`pyproject.toml` -- no new marker needed). This is the true end-to-end proof for
AC-BI-001/003/004/005/006 (issue #82, Slice 8.5, added by Repair as CHANGES.md C-01):
`ps-service` runs as a real OS subprocess, `ps_cli.cli.run()` runs in this test process
and makes a real HTTP round trip against it -- no mocks anywhere in this file.

Vendors its own copies of `_find_free_port`, `_spawn_ps_service`, `_wait_until_healthy`,
`_terminate`, and the `running_ps_service` fixture from
`test_integration_regulations_list.py:68-155`, per that file's own decoupling docstring
(lines 9-12) and L2 Project Structure's "ps-service and ps-cli are fully decoupled ...
each vendors its own copy of anything it needs" -- this file must never import anything
from `ps_service`.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from ps_cli.cli import run

if TYPE_CHECKING:
    from collections.abc import Iterator

if sys.platform == "win32":  # pragma: no cover - documented platform caveat, not exercised here
    pytest.skip("subprocess signal semantics differ on Windows", allow_module_level=True)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOST = "127.0.0.1"
# Widened from the `test_integration_regulations_list.py` original's 10.0s: measured
# directly in this sandbox, `ps_service` startup (LiteLLM's blocking warm-up call)
# reliably takes 13-15s, so 10s under-times here. 30s keeps the same bounded-poll
# design with headroom for this environment's real observed latency.
_READY_POLL_TIMEOUT_SECONDS = 30.0
_READY_POLL_INTERVAL_SECONDS = 0.05
_TERMINATE_WAIT_TIMEOUT_SECONDS = 10


def _find_free_port() -> int:
    """Bind a socket to port 0, read the OS-assigned port, close it, return the number.

    Same bind-close-reuse-port pattern as `test_integration_regulations_list.py`, with
    the same accepted small TOCTOU race (another process could claim the port between
    close and the subprocess's own bind) -- low-probability, not engineered away.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind((_HOST, 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _spawn_ps_service(port: int, log_dir: Path) -> subprocess.Popen[bytes]:
    """Spawn `python -m ps_service` directly (no `uv run` wrapper), bound to `port`.

    `PS_SERVICE_PORT` is the exact environment variable
    `ps_service.config.load_config()` reads. `PS_LOGGING_DIR` isolates this run's log
    sink to a throwaway directory. `sys.executable` resolves to the same shared
    workspace `.venv` interpreter this test process itself runs under, so `ps_service`
    is importable without any `uv run` indirection.
    """
    env = {**os.environ, "PS_SERVICE_PORT": str(port), "PS_LOGGING_DIR": str(log_dir)}
    return subprocess.Popen(
        [sys.executable, "-m", "ps_service"],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM the subprocess, escalating to SIGKILL if it doesn't exit promptly.

    Called from the fixture's `finally` block so the subprocess is reaped even when
    the test body raises or a health-poll timeout fires.
    """
    if proc.poll() is not None:
        return  # already exited
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=_TERMINATE_WAIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=_TERMINATE_WAIT_TIMEOUT_SECONDS)


def _wait_until_healthy(health_url: str) -> None:
    """Poll `GET /health` with a short bounded retry loop until it responds.

    Bounded, not an infinite loop: raises `TimeoutError` if the deadline is reached
    with no response at all, so a startup failure fails the test promptly instead of
    hanging. A response object at all (any status) means the server is accepting
    connections; `ps_service/main.py`'s `health()` handler only ever returns 200, so a
    non-exception response is equivalent to "healthy" here.
    """
    deadline = time.monotonic() + _READY_POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            response = httpx.get(health_url, timeout=1.0)
        except httpx.HTTPError:
            response = None
        if response is not None:
            return
        time.sleep(_READY_POLL_INTERVAL_SECONDS)
    msg = f"ps_service did not become healthy within {_READY_POLL_TIMEOUT_SECONDS}s"
    raise TimeoutError(msg)


@pytest.fixture
def running_ps_service(tmp_path: Path) -> Iterator[str]:
    """Spawn a real `ps_service` subprocess on a free port; yield its base URL.

    Tears down (SIGTERM, escalating to SIGKILL) in a `finally` block, so the
    subprocess is reaped even if the test body -- or the health poll itself -- raises.
    """
    port = _find_free_port()
    log_dir = tmp_path / "ps-service-logs"
    log_dir.mkdir()
    proc = _spawn_ps_service(port, log_dir)
    try:
        _wait_until_healthy(f"http://{_HOST}:{port}/health")
        yield f"http://{_HOST}:{port}"
    finally:
        _terminate(proc)


@pytest.mark.integration
def test_version_against_real_spawned_service_reports_matching_versions(
    running_ps_service: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-001/003/004/005 true happy path: `--version` against a real, spawned
    `ps-service`, over `PS_CLI_SERVICE_URL` -- a genuine HTTP round trip, no mocks.

    Both expected version strings are read in-process via `importlib.metadata.version`
    (never hardcoded), so this proves the real wire contract without coupling to
    whatever version happens to be installed.
    """
    monkeypatch.setenv("PS_CLI_SERVICE_URL", running_ps_service)

    exit_code = run(["--version"], client=None)

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert exit_code == 0
    assert len(lines) == 2
    assert lines[0] == f"PS-CLI Client Version: {installed_version('ps-cli')}"
    assert lines[1] == f"PS-Service Version: {installed_version('ps-service')}"


@pytest.mark.integration
def test_ac_bi_006_context_override_hits_the_real_named_service(
    running_ps_service: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-006's real-network proof: `--version --context dev` reaches the real,
    running `dev` server even while the *current* context ("prod") is unreachable.

    `dev` is a real spawned `ps_service` subprocess (`running_ps_service`); `prod`
    points at a definitely-closed local port -- bind a socket, close it, reuse the
    freed port number, mirroring `test_cli.py`'s existing bind-close pattern (e.g.
    `test_ac_bi_006_version_context_flag_targets_the_named_contexts_url_for_one_call`,
    `test_cli.py:1499-1507`). `config set-context`/`use-context` never construct a
    `PsServiceClient` (PLAN.md issue #56 D8), so `client=None` is safe for those calls.

    The control call (no `--context` override) proves the first call's success was
    load-bearing, not a coincidental default: with "prod" active and unreachable, the
    second stdout line must read "unavailable (...)".
    """
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)

    prod_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    prod_probe.bind((_HOST, 0))
    prod_port = prod_probe.getsockname()[1]
    prod_probe.close()

    run(["config", "set-context", "dev", "--url", running_ps_service], client=None)
    run(
        ["config", "set-context", "prod", "--url", f"http://{_HOST}:{prod_port}"],
        client=None,
    )
    run(["config", "use-context", "prod"], client=None)

    dev_exit_code = run(["--version", "--context", "dev"], client=None)
    dev_lines = capsys.readouterr().out.splitlines()

    assert dev_exit_code == 0
    assert len(dev_lines) == 2
    assert dev_lines[1] == f"PS-Service Version: {installed_version('ps-service')}"

    control_exit_code = run(["--version"], client=None)
    control_lines = capsys.readouterr().out.splitlines()

    assert control_exit_code == 0
    assert control_lines[1].startswith("PS-Service Version: unavailable (")
