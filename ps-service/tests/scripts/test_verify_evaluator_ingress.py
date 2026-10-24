"""Tests for `scripts/verify-evaluator-ingress.sh`.

The script runs for real against fake `kubectl`, `curl` and `podman` executables on a minimal PATH
(the external CLIs are the process boundary). The fakes answer from environment variables and log
their argv, so a test can prove the script only reads.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "verify-evaluator-ingress.sh"
SUBPROCESS_TIMEOUT_SECONDS = 30.0

FAKE_KUBECTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$FAKE_LOG"
case "$*" in
  "config current-context") printf '%s\\n' "${FAKE_CONTEXT:-kind-policy-system}" ;;
  *"get deployment ingress-nginx-controller"*) printf '%s' "${FAKE_READY_REPLICAS-1}" ;;
  *"get endpointslices"*) printf '%s' "${FAKE_ENDPOINTS-10.244.0.7}" ;;
esac
exit 0
"""

FAKE_CURL = """#!/usr/bin/env bash
printf 'curl %s\\n' "$*" >>"$FAKE_LOG"
printf '%s' "${FAKE_HTTP_CODE-404}"
exit "${FAKE_CURL_EXIT:-0}"
"""

FAKE_PODMAN = """#!/usr/bin/env bash
printf 'podman %s\\n' "$*" >>"$FAKE_LOG"
[[ "$1" == port ]] || exit 0
printf '%s' "${FAKE_PORTS-6443/tcp -> 127.0.0.1:45193
30300/tcp -> 127.0.0.1:3001
30443/tcp -> 0.0.0.0:30443
}"
exit 0
"""

TOOLS = ("bash", "env", "dirname", "basename", "cat", "grep", "sed", "tr", "sort", "cut", "head")


@dataclass(frozen=True)
class RunResult:
    code: int
    stdout: str
    stderr: str
    calls: list[str]

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run(tmp_path: Path, *, without: str | None = None, **env: str) -> RunResult:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in TOOLS:
        found = shutil.which(tool)
        assert found is not None
        (bin_dir / tool).symlink_to(found)
    fakes = {"kubectl": FAKE_KUBECTL, "curl": FAKE_CURL, "podman": FAKE_PODMAN}
    for name, body in fakes.items():
        if name != without:
            _write_executable(bin_dir / name, body)
    log = tmp_path / "calls.log"
    log.write_text("")
    bash = shutil.which("bash")
    assert bash is not None
    done = subprocess.run(  # noqa: S603 - bash is a shutil.which-resolved absolute path
        [bash, str(SCRIPT)],
        env={"PATH": str(bin_dir), "HOME": str(tmp_path), "FAKE_LOG": str(log), **env},
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    return RunResult(done.returncode, done.stdout, done.stderr, log.read_text().splitlines())


def test_every_check_passes_on_a_healthy_cluster(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result.code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 4
    assert all(line.startswith("PASS") for line in lines)


def test_it_only_reads(tmp_path: Path) -> None:
    result = _run(tmp_path)

    verbs = {call.split()[0] for call in result.calls if not call.startswith(("curl", "podman"))}
    forbidden = {"apply", "delete", "create", "patch", "label", "annotate", "scale", "edit"}
    assert not any(set(call.split()) & forbidden for call in result.calls)
    assert verbs <= {"config", "-n", "get"}
    assert any(call.startswith("podman port policy-system-control-plane") for call in result.calls)


def test_the_node_container_follows_the_cluster_name(tmp_path: Path) -> None:
    result = _run(tmp_path, FAKE_CONTEXT="kind-eval")

    assert "podman port eval-control-plane" in result.calls


def test_a_controller_that_is_not_ready_fails_that_check_only(tmp_path: Path) -> None:
    result = _run(tmp_path, FAKE_READY_REPLICAS="")

    assert result.code == 1
    assert sum(line.startswith("FAIL") for line in result.output.splitlines()) == 1
    assert "controller" in next(
        line for line in result.output.splitlines() if line.startswith("FAIL")
    )


def test_a_missing_admission_endpoint_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, FAKE_ENDPOINTS="")

    assert result.code == 1
    assert "FAIL" in result.output
    assert "admission" in result.output


@pytest.mark.parametrize("code", ["200", "502", "000"])
def test_anything_but_404_at_the_root_fails(tmp_path: Path, code: str) -> None:
    result = _run(tmp_path, FAKE_HTTP_CODE=code)

    assert result.code == 1
    assert "30443" in next(line for line in result.output.splitlines() if line.startswith("FAIL"))


def test_the_root_request_goes_to_loopback_on_the_ingress_port(tmp_path: Path) -> None:
    result = _run(tmp_path)

    curl = next(call for call in result.calls if call.startswith("curl"))
    assert "https://127.0.0.1:30443/" in curl


def test_a_host_port_open_beyond_the_loopback_fails(tmp_path: Path) -> None:
    ports = "6443/tcp -> 127.0.0.1:45193\n30300/tcp -> 127.0.0.1:3001\n30443/tcp -> 0.0.0.0:30443\n"
    result = _run(tmp_path, FAKE_PORTS=ports + "80/tcp -> 0.0.0.0:80\n")

    assert result.code == 1
    assert "80" in next(line for line in result.output.splitlines() if line.startswith("FAIL"))


def test_the_falkordb_browser_port_open_beyond_the_loopback_fails(tmp_path: Path) -> None:
    ports = "6443/tcp -> 127.0.0.1:45193\n30300/tcp -> 0.0.0.0:3001\n30443/tcp -> 0.0.0.0:30443\n"
    result = _run(tmp_path, FAKE_PORTS=ports)

    assert result.code == 1
    assert "3001" in next(line for line in result.output.splitlines() if line.startswith("FAIL"))


def test_the_loopback_api_server_port_is_allowed(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result.code == 0
    assert "6443" not in result.output


def test_a_missing_ingress_port_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, FAKE_PORTS="6443/tcp -> 127.0.0.1:45193\n30300/tcp -> 127.0.0.1:3001\n")

    assert result.code == 1
    assert "30443" in result.output


def test_a_non_kind_context_is_refused_before_any_check(tmp_path: Path) -> None:
    result = _run(tmp_path, FAKE_CONTEXT="aks-prod")

    assert result.code == 1
    assert "kind-" in result.stderr
    assert not any(call.startswith(("curl", "podman")) for call in result.calls)


def test_a_missing_container_engine_fails_the_port_check(tmp_path: Path) -> None:
    result = _run(tmp_path, without="podman")

    assert result.code == 1
    assert "podman" in result.output
