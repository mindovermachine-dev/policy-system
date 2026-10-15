"""Tests for `scripts/verify-backup-restore.sh backup`.

The script runs for real against a fake `kubectl` on PATH (the external CLI is the process
boundary). The fake records its argv, answers `psql` count queries from a counter file that
advances after each `pg_basebackup`, and streams a real tar for `pg_basebackup`.
"""

from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "verify-backup-restore.sh"

SUBPROCESS_TIMEOUT_SECONDS = 30.0

EXPECTED_BACKUP_ARGS = "pg_basebackup -U postgres_admin -D - -Ft -X fetch -c fast"

FAKE_KUBECTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$FAKE_KUBECTL_LOG"
if [[ "$1 $2" == "config current-context" ]]; then
  printf '%s\\n' "$FAKE_CONTEXT"
  exit 0
fi
args="$*"
if [[ "$args" == *pg_basebackup* ]]; then
  cat "$FAKE_BACKUP_PAYLOAD"
  exit "${FAKE_BACKUP_EXIT:-0}"
fi
if [[ "$args" == *psql* ]]; then
  count="$(cat "$FAKE_COUNTER")"
  printf '%s\\n' "$count"
  exit 0
fi
exit 99
"""


@dataclass(frozen=True)
class RunResult:
    code: int
    stdout: str
    stderr: str
    kubectl_calls: list[str]
    out_dir: Path


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _valid_tar() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        data = b"16\n"
        info = tarfile.TarInfo("PG_VERSION")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _run_backup(
    tmp_path: Path,
    *,
    context: str = "kind-policy-system",
    payload: bytes | None = None,
    backup_exit: int = 0,
    extra_args: tuple[str, ...] = (),
) -> RunResult:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "kubectl", FAKE_KUBECTL)
    log = tmp_path / "kubectl.log"
    log.write_text("")
    counter = tmp_path / "counter"
    counter.write_text("7")
    payload_file = tmp_path / "payload.tar"
    payload_file.write_bytes(_valid_tar() if payload is None else payload)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    bash = shutil.which("bash")
    assert bash is not None
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CONTEXT": context,
        "FAKE_KUBECTL_LOG": str(log),
        "FAKE_BACKUP_PAYLOAD": str(payload_file),
        "FAKE_BACKUP_EXIT": str(backup_exit),
        "FAKE_COUNTER": str(counter),
    }
    result = subprocess.run(  # noqa: S603 - bash is a shutil.which-resolved absolute path; args are literals
        [bash, str(SCRIPT), "backup", "--out-dir", str(out_dir), *extra_args],
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    return RunResult(
        result.returncode, result.stdout, result.stderr, log.read_text().splitlines(), out_dir
    )


def test_backup_runs_one_pg_basebackup_and_writes_one_verified_tar(tmp_path: Path) -> None:
    result = _run_backup(tmp_path)

    assert result.code == 0, result.stderr
    backups = [call for call in result.kubectl_calls if "pg_basebackup" in call]
    assert len(backups) == 1
    assert EXPECTED_BACKUP_ARGS in backups[0]
    assert "deploy/policy-system-ps-postgres" in backups[0]
    artifacts = list(result.out_dir.glob("ps-postgres-*.tar"))
    assert len(artifacts) == 1
    assert tarfile.is_tarfile(artifacts[0])
    assert result.stdout.strip() == str(artifacts[0])
    assert "event=backup_verified" in result.stderr


def test_backup_logs_pre_and_post_counts(tmp_path: Path) -> None:
    result = _run_backup(tmp_path)

    assert result.code == 0, result.stderr
    assert "event=counts phase=pre audit_events=7" in result.stderr
    assert "event=counts phase=post audit_events=7" in result.stderr


def test_backup_counts_the_audit_trail_from_its_audit_log_schema(tmp_path: Path) -> None:
    result = _run_backup(tmp_path)

    assert result.code == 0, result.stderr
    count_queries = [call for call in result.kubectl_calls if "SELECT count(*)" in call]
    assert any("FROM audit_log.audit_events" in call for call in count_queries)
    assert not any(call.endswith("FROM audit_events") for call in count_queries)


def test_backup_refuses_a_non_kind_context_without_allow_context(tmp_path: Path) -> None:
    result = _run_backup(tmp_path, context="aks-prod")

    assert result.code == 1
    assert "aks-prod" in result.stderr
    assert not [call for call in result.kubectl_calls if "pg_basebackup" in call]


def test_backup_accepts_a_non_kind_context_named_by_allow_context(tmp_path: Path) -> None:
    result = _run_backup(tmp_path, context="aks-prod", extra_args=("--allow-context", "aks-prod"))

    assert result.code == 0, result.stderr


def test_backup_fails_and_leaves_no_artifact_when_the_exec_fails(tmp_path: Path) -> None:
    result = _run_backup(tmp_path, backup_exit=1)

    assert result.code == 1
    assert "event=backup_failed" in result.stderr
    assert list(result.out_dir.iterdir()) == []


def test_backup_fails_and_leaves_no_artifact_when_the_output_is_empty(tmp_path: Path) -> None:
    result = _run_backup(tmp_path, payload=b"")

    assert result.code == 1
    assert "event=backup_failed" in result.stderr
    assert list(result.out_dir.iterdir()) == []


def test_backup_fails_and_leaves_no_artifact_when_the_output_is_not_a_tar(tmp_path: Path) -> None:
    result = _run_backup(tmp_path, payload=b"pg_basebackup: error: not a tar" * 100)

    assert result.code == 1
    assert "event=backup_failed" in result.stderr
    assert list(result.out_dir.iterdir()) == []


def test_unknown_subcommand_is_rejected(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(  # noqa: S603 - bash is a shutil.which-resolved absolute path; args are literals
        [bash, str(SCRIPT), "frobnicate"],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
        cwd=tmp_path,
    )

    assert result.returncode == 1
    assert "Usage" in result.stderr


@pytest.mark.parametrize("flag", ["-D -", "-Ft", "-X fetch", "-c fast"])
def test_script_carries_the_documented_flag_set(flag: str) -> None:
    assert flag in SCRIPT.read_text()
