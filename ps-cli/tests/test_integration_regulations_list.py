"""Integration test: `ps-cli regulations list` against a real spawned `ps-service`.

Marked `@pytest.mark.integration` only (the marker is already registered in the root
`pyproject.toml` -- no new marker needed). This is the true end-to-end proof for
AC-BI-001 (PLAN.md §3 Increment 16): `ps-service` runs as a real OS subprocess,
`ps_cli.cli.run()` runs in this test process and makes a real HTTP round trip against
it over `PS_CLI_SERVICE_URL` -- no mocks anywhere in this test.

Vendors its own `_spawn_ps_service()` / `_wait_until_healthy()` helpers, adapted from
`ps-service/tests/test_main_integration.py`'s subprocess-spawn/health-poll pattern --
copied, not imported cross-member, per L2 Project Structure's "ps-service and ps-cli
are fully decoupled ... each vendors its own copy of anything it needs".

This file must never import anything from `ps_service` -- per PLAN.md §3 Increment 16 /
OPEN_QUESTIONS_RESOLVED.md item 3, the expected CELEX+title catalog pairs below are
hardcoded as literal fixture data instead (copied by hand from
`ps_service/src/ps_service/api/catalog.py::REGULATION_CATALOG`, read-only reference,
not imported). If `ps-service`'s curated catalog ever drifts from this hardcoded list,
this test is expected to fail loudly -- that is the intended drift-catching behavior,
not a bug.

No FalkorDB/LLM dependency is needed: `GET /regulations` serves a static, in-memory
catalog. Polling `GET /health` (liveness) is sufficient to know the server is up --
confirmed by reading `ps_service/main.py`'s `health()` handler, which returns
`{"status": "alive"}` unconditionally, never checking external dependencies -- `/ready`
(which does check FalkorDB/LLM/Cellar-ELI) is not needed here.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
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
_READY_POLL_TIMEOUT_SECONDS = 10.0
_READY_POLL_INTERVAL_SECONDS = 0.05
_TERMINATE_WAIT_TIMEOUT_SECONDS = 10

# Hardcoded (celex, title) pairs, copied by hand from
# `ps_service/src/ps_service/api/catalog.py::REGULATION_CATALOG` -- deliberately NOT
# imported (see module docstring). `short_name`/`version` never cross the REST
# boundary, so they are not part of this fixture.
_EXPECTED_CATALOG: tuple[tuple[str, str], ...] = (
    ("32024R2847", "Cyber Resilience Act"),
    ("32016R0679", "General Data Protection Regulation"),
    ("32022L2555", "NIS2 Directive"),
)


def _find_free_port() -> int:
    """Bind a socket to port 0, read the OS-assigned port, close it, return the number.

    Same bind-close-reuse-port pattern as PLAN.md Increment 13, with the same accepted
    small TOCTOU race (another process could claim the port between close and the
    subprocess's own bind) -- low-probability, not engineered away.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind((_HOST, 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _spawn_ps_service(port: int, log_dir: Path) -> subprocess.Popen[bytes]:
    """Spawn `python -m ps_service` directly (no `uv run` wrapper), bound to `port`.

    `PS_SERVICE_PORT` is the exact environment variable
    `ps_service.config.load_config()` reads (confirmed by reading
    `ps_service/src/ps_service/config.py` -- not guessed). `PS_LOGGING_DIR` isolates
    this run's log sink to a throwaway directory. `sys.executable` resolves to the
    same shared workspace `.venv` interpreter this test process itself runs under, so
    `ps_service` is importable without any `uv run` indirection.
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


def _parse_catalog_line(line: str) -> tuple[str, str]:
    """Split one `handle_regulations_list`-formatted stdout line into `(celex, title)`.

    Mirrors `handle_regulations_list`'s exact `f"{celex}  {title}"` format (two
    spaces) -- this is what makes the parse a real assertion on the wire contract's
    output, not a loose substring check.
    """
    celex, _, title = line.partition("  ")
    return celex, title


@pytest.mark.integration
def test_regulations_list_against_real_spawned_ps_service(
    running_ps_service: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-001 true happy path: `regulations list` against a real, spawned ps-service.

    Only `ps-service` runs as a real OS subprocess; `ps_cli.cli.run()` runs in this
    test process and makes a real HTTP round trip against it over
    `PS_CLI_SERVICE_URL` -- sufficient to prove the real REST wire contract end to end
    without the complexity of double-subprocessing. No FalkorDB/LLM dependency needed.
    """
    monkeypatch.setenv("PS_CLI_SERVICE_URL", running_ps_service)

    exit_code = run(["regulations", "list"], client=None)

    captured = capsys.readouterr()
    assert exit_code == 0
    printed_lines = [line for line in captured.out.splitlines() if line]
    printed_pairs = tuple(_parse_catalog_line(line) for line in printed_lines)
    assert printed_pairs == _EXPECTED_CATALOG
