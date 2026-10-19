"""Tests for `scripts/measure-proxy-footprint.sh`.

The script runs for real against fake `kubectl` and `curl` executables on PATH (the external
CLIs are the process boundary). The fakes record their argv and answer from environment
switches, so the metrics-server and cgroup paths are both exercised.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "measure-proxy-footprint.sh"

SUBPROCESS_TIMEOUT_SECONDS = 60.0
POD = "policy-system-proxy-7d9f"

FAKE_KUBECTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$FAKE_KUBECTL_LOG"
args="$*"
if [[ "$args" == *"get pods"* ]]; then
  printf '%s' "${FAKE_POD-policy-system-proxy-7d9f}"
  exit 0
fi
if [[ "$args" == *"top pod"* ]]; then
  if [[ -n "${FAKE_NO_METRICS:-}" ]]; then
    echo "error: Metrics API not available" >&2
    exit 1
  fi
  printf '%s %s %s\\n' "policy-system-proxy-7d9f" "${FAKE_TOP_CPU:-3m}" "${FAKE_TOP_MEM:-21Mi}"
  exit 0
fi
if [[ "$args" == *"top node"* ]]; then
  printf '%s\\n' "kind-control-plane 250m 6% 2100Mi 27%"
  exit 0
fi
if [[ "$args" == *"exec"* && "$args" == *memory.current* ]]; then
  printf '%s\\n' "${FAKE_CGROUP_MEMORY:-33554432}"
  exit 0
fi
if [[ "$args" == *"exec"* && "$args" == *cpu.stat* ]]; then
  count_file="$FAKE_KUBECTL_LOG.cpu"
  n="$(cat "$count_file" 2>/dev/null || echo 0)"
  echo "$((n + 1))" >"$count_file"
  printf 'usage_usec %s\\nuser_usec 1\\n' "$((n * 2000000))"
  exit 0
fi
exit 99
"""

FAKE_CURL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$FAKE_CURL_LOG"
sleep 0.1
exit 0
"""


@dataclass(frozen=True)
class RunResult:
    code: int
    stdout: str
    stderr: str
    kubectl_calls: list[str]
    curl_calls: list[str]
    output_file: Path


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run(
    tmp_path: Path,
    args: tuple[str, ...] = ("--url", "https://ps.test:30443"),
    extra_env: dict[str, str] | None = None,
) -> RunResult:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "kubectl", FAKE_KUBECTL)
    _write_executable(bin_dir / "curl", FAKE_CURL)
    kubectl_log = tmp_path / "kubectl.log"
    curl_log = tmp_path / "curl.log"
    kubectl_log.write_text("")
    curl_log.write_text("")
    output_file = tmp_path / "footprint.md"

    bash = shutil.which("bash")
    assert bash is not None
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_KUBECTL_LOG": str(kubectl_log),
        "FAKE_CURL_LOG": str(curl_log),
        **(extra_env or {}),
    }
    result = subprocess.run(  # noqa: S603 - bash is a shutil.which-resolved absolute path; args are literals
        [bash, str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    return RunResult(
        result.returncode,
        result.stdout,
        result.stderr,
        kubectl_log.read_text().splitlines(),
        curl_log.read_text().splitlines(),
        output_file,
    )


def _fast(*extra: str) -> tuple[str, ...]:
    return ("--url", "https://ps.test:30443", "--idle-seconds", "0", "--load-seconds", "1", *extra)


def test_measure_proxy_footprint_prints_row_from_top_output(tmp_path: Path) -> None:
    result = _run(tmp_path, _fast("--concurrency", "2"))

    assert result.code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "| Phase | Pod | CPU | Memory | Source |"
    assert lines[1] == "| --- | --- | --- | --- | --- |"
    assert f"| idle (0s) | {POD} | 3m | 21Mi | top |" in lines
    assert any(
        line.startswith("| load (2 x GET /health, 1s) | ") and "| 3m | 21Mi | top |" in line
        for line in lines
    )
    assert any(line.startswith("| node kind-control-plane (whole) | - | 250m |") for line in lines)


def test_measure_proxy_footprint_reports_missing_metrics_server(tmp_path: Path) -> None:
    result = _run(tmp_path, _fast(), {"FAKE_NO_METRICS": "1"})

    assert result.code == 0, result.stderr
    assert "metrics-server is not available" in result.stderr
    assert "Fix:" in result.stderr
    assert f"| idle (0s) | {POD} | n/a | 32Mi | cgroup |" in result.stdout
    assert "| cgroup |" in result.stdout.splitlines()[-1]
    assert not any("top node" in call for call in result.kubectl_calls)


def test_measure_proxy_footprint_cgroup_cpu_is_the_mean_over_the_window(tmp_path: Path) -> None:
    result = _run(tmp_path, _fast(), {"FAKE_NO_METRICS": "1"})

    assert result.code == 0, result.stderr
    assert "| 2000m | 32Mi | cgroup |" in result.stdout


def test_measure_proxy_footprint_selects_the_proxy_pod_and_namespace(tmp_path: Path) -> None:
    result = _run(tmp_path, _fast("--namespace", "eval", "--context", "kind-x"))

    assert result.code == 0, result.stderr
    first = result.kubectl_calls[0]
    assert "--context kind-x" in first
    assert "-n eval" in first
    assert "app.kubernetes.io/component=proxy" in first


def test_measure_proxy_footprint_load_hits_health_through_the_given_url(tmp_path: Path) -> None:
    ca_file = tmp_path / "ca.pem"

    result = _run(tmp_path, _fast("--cacert", str(ca_file), "--concurrency", "3"))

    assert result.code == 0, result.stderr
    assert result.curl_calls
    assert all("https://ps.test:30443/health" in call for call in result.curl_calls)
    assert all(f"--cacert {ca_file}" in call for call in result.curl_calls)


def test_measure_proxy_footprint_writes_the_table_to_output(tmp_path: Path) -> None:
    out = tmp_path / "footprint.md"

    result = _run(tmp_path, _fast("--output", str(out)))

    assert result.code == 0, result.stderr
    assert out.read_text() == result.stdout


def test_measure_proxy_footprint_requires_a_url(tmp_path: Path) -> None:
    result = _run(tmp_path, ())

    assert result.code == 1
    assert "--url is required" in result.stderr
    assert result.kubectl_calls == []


def test_measure_proxy_footprint_rejects_a_non_numeric_duration(tmp_path: Path) -> None:
    result = _run(tmp_path, ("--url", "https://ps.test:30443", "--load-seconds", "soon"))

    assert result.code == 1
    assert "whole numbers" in result.stderr


def test_measure_proxy_footprint_rejects_an_unknown_flag(tmp_path: Path) -> None:
    result = _run(tmp_path, ("--bogus",))

    assert result.code == 1
    assert "unknown argument" in result.stderr


def test_measure_proxy_footprint_fails_when_no_proxy_pod_exists(tmp_path: Path) -> None:
    result = _run(tmp_path, _fast(), {"FAKE_POD": ""})

    assert result.code == 1
    assert "no proxy pod found" in result.stderr
    assert "Fix:" in result.stderr
    assert result.curl_calls == []


def test_measure_proxy_footprint_only_reads_the_cluster(tmp_path: Path) -> None:
    result = _run(tmp_path, _fast(), {"FAKE_NO_METRICS": "1"})

    assert result.kubectl_calls
    verbs = {call.split()[call.split().index("-n") + 2] for call in result.kubectl_calls}
    assert verbs <= {"get", "top", "exec"}
    assert all("cat /sys/fs/cgroup" in c for c in result.kubectl_calls if " exec " in c)
