"""Tests for `scripts/ps-service.sh start | status | stop`.

The script runs for real from a copy under `tmp_path` (it derives its repo root from its own
location, so its pid file and logs land there). The external commands are the process boundary:
a fake `uv` on PATH stands in for the service process (it only sleeps, so `start` gets a live
pid), and a fake `curl` records every URL it is asked for and answers the local probes.
"""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "ps-service.sh"

SUBPROCESS_TIMEOUT_SECONDS = 30.0
PORT = "18731"
RETIRED_PROBE_PATHS = tuple(f"/{name}" for name in ("health", "ready"))

FAKE_UV = """#!/usr/bin/env bash
exec sleep 30
"""

FAKE_CURL = """#!/usr/bin/env bash
url="${@: -1}"
printf '%s\\n' "$url" >>"$FAKE_CURL_LOG"
case "$url" in
  */livez) printf '{"status":"alive"}'; exit 0 ;;
  */readyz) printf '{"status":"ready"}'; exit 0 ;;
esac
exit 22
"""


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class _Sandbox:
    """A copy of the script in `tmp_path`, with fake `uv` and `curl` first on PATH."""

    def __init__(self, tmp_path: Path) -> None:
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        self.script = scripts / "ps-service.sh"
        shutil.copy2(SCRIPT, self.script)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_executable(bin_dir / "uv", FAKE_UV)
        _write_executable(bin_dir / "curl", FAKE_CURL)
        self.curl_log = tmp_path / "curl.log"
        self.pid_file = tmp_path / "logs" / "ps-service.pid"
        self.env = {
            key: value for key, value in os.environ.items() if not key.startswith("PS_SERVICE_")
        }
        self.env.update(
            {
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "FAKE_CURL_LOG": str(self.curl_log),
                "PS_SERVICE_PORT": PORT,
                "PS_SERVICE_STARTUP_TIMEOUT_SECONDS": "5",
                "PS_SERVICE_SHUTDOWN_TIMEOUT_SECONDS": "5",
            }
        )

    def run(self, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 -- fixed argv: bash and the sandboxed script copy
            ["bash", str(self.script), command],  # noqa: S607 -- bash from PATH, as the script's shebang does
            env=self.env,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )

    def requested_urls(self) -> list[str]:
        if not self.curl_log.exists():
            return []
        return self.curl_log.read_text(encoding="utf-8").splitlines()

    def kill_leftover(self) -> None:
        if not self.pid_file.exists():
            return
        try:
            os.kill(int(self.pid_file.read_text(encoding="utf-8").strip()), signal.SIGKILL)
        except ProcessLookupError, ValueError:
            return


def _assert_no_retired_probe_path(urls: list[str]) -> None:
    for url in urls:
        for retired in RETIRED_PROBE_PATHS:
            assert not url.endswith(retired), url


def test_start_polls_livez_on_the_local_port(tmp_path: Path) -> None:
    sandbox = _Sandbox(tmp_path)
    try:
        started = sandbox.run("start")
    finally:
        sandbox.run("stop")
        sandbox.kill_leftover()

    assert started.returncode == 0, started.stderr
    urls = sandbox.requested_urls()
    assert urls[0] == f"http://127.0.0.1:{PORT}/livez"
    assert f"http://127.0.0.1:{PORT}/readyz" in urls
    assert '"status":"ready"' in started.stdout
    _assert_no_retired_probe_path(urls)


def test_status_reports_livez_and_readyz(tmp_path: Path) -> None:
    sandbox = _Sandbox(tmp_path)
    try:
        assert sandbox.run("start").returncode == 0
        sandbox.curl_log.unlink()
        reported = sandbox.run("status")
    finally:
        sandbox.run("stop")
        sandbox.kill_leftover()

    assert reported.returncode == 0, reported.stderr
    assert sandbox.requested_urls() == [
        f"http://127.0.0.1:{PORT}/livez",
        f"http://127.0.0.1:{PORT}/readyz",
    ]
    assert "/livez: alive" in reported.stdout
    assert '/readyz: "status":"ready"' in reported.stdout
