"""Tests for `scripts/verify-backup-restore.sh seed | e2e` (issue #216, slice V3).

The script runs for real against fake `kubectl` and `helm` on PATH (the external CLIs are the
process boundary). The fake `kubectl` records its argv and answers: the helper-pod exec (a volume
wipe), the `tar` exec (the restore), the in-pod `python -` execs that carry the seed and digest
tools on stdin, and `psql` for the logged graph list and the row counts. Which digest snapshot and
which counts the fake returns depends on whether the data was restored, so a skipped or wrong
restore is visible to the comparison. The digest `compare` runs the real tool through `uv run`.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "verify-backup-restore.sh"
TOOLS = REPO_ROOT / "tools" / "backup-restore-verify"

SUBPROCESS_TIMEOUT_SECONDS = 120.0
READY_OK = '200 {"status":"ready","unhealthy_dependencies":[],"gated_graphs":[]}'
RETAINED_VALUES = {
    "psPostgres": {
        "admin": {"existingSecret": "ps-admin-retained"},
        "state": {"existingSecret": "ps-state-retained"},
        "signing": {"existingSecret": "ps-signing-retained"},
    }
}
DIGESTS = {"alpha": "sha256:" + "a" * 64, "beta": "sha256:" + "b" * 64}
CHANGED_DIGESTS = {"alpha": DIGESTS["alpha"], "beta": "sha256:" + "c" * 64}
EMPTY_DIGESTS = {"alpha": "sha256:" + "0" * 64, "beta": "sha256:" + "0" * 64}
PAYLOAD = b"artifact-payload"

FAKE_KUBECTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$FAKE_KUBECTL_LOG"
args="$*"
case "$args" in
  "config current-context") printf '%s\\n' "$FAKE_CONTEXT"; exit 0 ;;
  "get secret "*) exit 0 ;;
  "get deploy/policy-system-ps-postgres "*) printf 'postgres:16-alpine\\n'; exit 0 ;;
  "get job "*) printf 'True'; exit 0 ;;
  "logs deploy/policy-system-ps-postgres"*) printf 'database system is ready\\n'; exit 0 ;;
  "apply -f -") cat >/dev/null; touch "$FAKE_DIR/helper-applied"; exit 0 ;;
  *"exec -i"*"tar -xf"*) cat >/dev/null; touch "$FAKE_DIR/restored"; exit 0 ;;
  *"exec policy-system-restore-helper"*) exit 0 ;;
  *pg_basebackup*) cat "$FAKE_TAR"; exit 0 ;;
  *"exec -i deploy/policy-system-ps-service -- python - snapshot"*)
    cat >/dev/null
    if [[ ! -e "$FAKE_DIR/helper-applied" ]]; then cat "$FAKE_DIGESTS_BEFORE"
    elif [[ -e "$FAKE_DIR/restored" ]]; then cat "$FAKE_DIGESTS_AFTER"
    else cat "$FAKE_DIGESTS_EMPTY"; fi
    exit 0 ;;
  *"exec -i deploy/policy-system-ps-service -- python -"*)
    cat >>"$FAKE_DIR/seed-stdin.txt"; exit 0 ;;
  *"exec deploy/policy-system-ps-service"*) printf '%s\\n' "$FAKE_READY"; exit 0 ;;
  *"psql"*"DISTINCT graph"*) printf 'alpha\\nbeta\\n'; exit 0 ;;
  *psql*)
    if [[ -e "$FAKE_DIR/restored" ]]; then printf '%s\\n' "$FAKE_COUNT_AFTER"
    else printf '%s\\n' "$FAKE_COUNT_BEFORE"; fi
    exit 0 ;;
esac
exit 0
"""

FAKE_HELM = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$FAKE_HELM_LOG"
case "$*" in
  "get values"*) cat "$FAKE_HELM_VALUES"; exit 0 ;;
  "status"*) printf '{"version": 8}\\n'; exit 0 ;;
esac
exit 0
"""


@dataclass(frozen=True)
class RunResult:
    code: int
    stdout: str
    stderr: str
    kubectl: list[str]
    helm: list[str]
    seed_stdin: str


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run(
    tmp_path: Path,
    args: list[str],
    *,
    context: str = "kind-policy-system",
    stdin: str = "yes\n",
    helm_values: dict[str, object] | None = None,
    after_digests: dict[str, str] | None = None,
    count_before: str = "7",
    count_after: str = "7",
) -> RunResult:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "kubectl", FAKE_KUBECTL)
    _write_executable(bin_dir / "helm", FAKE_HELM)
    work = tmp_path / "work"
    work.mkdir()
    kubectl_log = work / "kubectl.log"
    helm_log = work / "helm.log"
    kubectl_log.write_text("")
    helm_log.write_text("")
    values = work / "values.json"
    values.write_text(json.dumps(RETAINED_VALUES if helm_values is None else helm_values))
    for name, content in (
        ("before.json", DIGESTS),
        ("after.json", DIGESTS if after_digests is None else after_digests),
        ("empty.json", EMPTY_DIGESTS),
    ):
        (work / name).write_text(json.dumps(content))
    (work / "source").mkdir()
    (work / "source" / "PG_VERSION").write_bytes(PAYLOAD)
    tar = shutil.which("tar")
    assert tar is not None
    subprocess.run(  # noqa: S603 - tar is a shutil.which-resolved absolute path; args are literals
        [tar, "cf", str(work / "backup.tar"), "-C", str(work / "source"), "."],
        check=True,
        timeout=30,
    )
    bash = shutil.which("bash")
    assert bash is not None
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DIR": str(work),
        "FAKE_CONTEXT": context,
        "FAKE_KUBECTL_LOG": str(kubectl_log),
        "FAKE_HELM_LOG": str(helm_log),
        "FAKE_HELM_VALUES": str(values),
        "FAKE_DIGESTS_BEFORE": str(work / "before.json"),
        "FAKE_DIGESTS_AFTER": str(work / "after.json"),
        "FAKE_DIGESTS_EMPTY": str(work / "empty.json"),
        "FAKE_TAR": str(work / "backup.tar"),
        "FAKE_READY": READY_OK,
        "FAKE_COUNT_BEFORE": count_before,
        "FAKE_COUNT_AFTER": count_after,
    }
    result = subprocess.run(  # noqa: S603 - bash is a shutil.which-resolved absolute path; args are literals
        [bash, str(SCRIPT), *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
        cwd=tmp_path,
    )
    seed_file = work / "seed-stdin.txt"
    return RunResult(
        result.returncode,
        result.stdout,
        result.stderr,
        kubectl_log.read_text().splitlines(),
        helm_log.read_text().splitlines(),
        seed_file.read_text() if seed_file.exists() else "",
    )


def _e2e_args(tmp_path: Path, *extra: str) -> list[str]:
    return ["e2e", "--out-dir", str(tmp_path / "out"), "--interval", "0.05", *extra]


def _index(calls: list[str], needle: str, *, start: int = 0) -> int:
    for position in range(start, len(calls)):
        if needle in calls[position]:
            return position
    message = f"no kubectl call at or after {start} contains {needle!r}: {calls}"
    raise AssertionError(message)


def test_seed_pipes_both_seed_tools_into_the_service_pod_with_the_instruments(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, ["seed", "--instrument", "A-1.0", "--instrument", "B-1.0"])

    assert result.code == 0, result.stderr
    content = _index(result.kubectl, "exec -i deploy/policy-system-ps-service -- python -")
    assert "--instrument A-1.0 --instrument B-1.0" in result.kubectl[content]
    grants = _index(
        result.kubectl, "exec -i deploy/policy-system-ps-service -- python -", start=content + 1
    )
    assert grants > content
    assert result.seed_stdin == (
        (TOOLS / "seed_content.py").read_text() + (TOOLS / "seed_grants.py").read_text()
    )


def test_seed_refuses_a_non_kind_context(tmp_path: Path) -> None:
    result = _run(tmp_path, ["seed"], context="aks-prod")

    assert result.code == 1
    assert not any("python -" in call for call in result.kubectl)


def test_e2e_runs_seed_snapshot_backup_wipe_restore_snapshot_in_order(tmp_path: Path) -> None:
    result = _run(tmp_path, _e2e_args(tmp_path))

    assert result.code == 0, result.stderr
    calls = result.kubectl
    seed = _index(calls, "exec -i deploy/policy-system-ps-service -- python -")
    graphs = _index(calls, "DISTINCT graph")
    before = _index(calls, "python - snapshot")
    backup = _index(calls, "pg_basebackup")
    wipe = _index(calls, "exec policy-system-restore-helper")
    extract = _index(calls, "tar -xf")
    scale_up = _index(calls, "scale deploy/policy-system-ps-service --replicas=1")
    after = _index(calls, "python - snapshot", start=before + 1)
    assert seed < graphs < before < backup < wipe < extract < scale_up < after
    assert _index(calls, "scale deploy/policy-system-ps-postgres --replicas=0") < wipe
    helm_upgrade = [line for line in result.helm if line.startswith("upgrade")]
    assert len(helm_upgrade) == 1
    assert "--wait-for-jobs" in helm_upgrade[0]
    assert "event=e2e_verified" in result.stderr


def test_e2e_snapshots_the_logged_graphs_before_and_after(tmp_path: Path) -> None:
    result = _run(tmp_path, _e2e_args(tmp_path))

    snapshots = [call for call in result.kubectl if "python - snapshot" in call]
    assert len(snapshots) == 2
    for call in snapshots:
        assert "--graph alpha --graph beta" in call
    assert sum("DISTINCT graph" in call for call in result.kubectl) == 1


def test_e2e_wipes_both_volumes_before_the_restore(tmp_path: Path) -> None:
    result = _run(tmp_path, _e2e_args(tmp_path))

    wipes = [call for call in result.kubectl if "exec policy-system-restore-helper" in call]
    assert wipes
    first = wipes[0]
    assert "/pg/" in first
    assert "/fdb/" in first


def test_e2e_exits_one_and_names_the_graph_when_a_digest_differs(tmp_path: Path) -> None:
    result = _run(tmp_path, _e2e_args(tmp_path), after_digests=CHANGED_DIGESTS)

    assert result.code == 1
    assert "graph beta" in result.stderr
    assert "event=e2e_verified" not in result.stderr


def test_e2e_exits_one_when_a_count_differs(tmp_path: Path) -> None:
    result = _run(tmp_path, _e2e_args(tmp_path), count_before="7", count_after="8")

    assert result.code == 1
    assert "event=e2e_verified" not in result.stderr
    assert "audit_events" in result.stderr


def test_e2e_skip_restore_leaves_the_data_unrestored_and_fails_the_comparison(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, _e2e_args(tmp_path, "--skip-restore"))

    assert result.code == 1
    assert not any("tar -xf" in call for call in result.kubectl)
    assert "graph alpha" in result.stderr


def test_e2e_refuses_a_non_kind_context_without_touching_the_cluster(tmp_path: Path) -> None:
    result = _run(tmp_path, _e2e_args(tmp_path), context="aks-prod")

    assert result.code == 1
    assert not any("python -" in call or "scale" in call for call in result.kubectl)


def test_e2e_refuses_without_the_typed_confirmation(tmp_path: Path) -> None:
    result = _run(tmp_path, _e2e_args(tmp_path), stdin="no\n")

    assert result.code == 1
    assert not any("python -" in call or "scale" in call for call in result.kubectl)


def test_e2e_yes_flag_skips_the_confirmation(tmp_path: Path) -> None:
    result = _run(tmp_path, _e2e_args(tmp_path, "--yes"), stdin="")

    assert result.code == 0, result.stderr


def test_e2e_refuses_when_credentials_secrets_are_not_retained(tmp_path: Path) -> None:
    result = _run(tmp_path, _e2e_args(tmp_path), helm_values={})

    assert result.code == 1
    assert not any("python -" in call for call in result.kubectl)
