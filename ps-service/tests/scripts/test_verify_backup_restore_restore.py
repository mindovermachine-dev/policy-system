"""Tests for `scripts/verify-backup-restore.sh restore | wait-ready | verify-provision`.

The script runs for real against fake `kubectl`, `helm` and `jq`-free helpers on PATH (the
external CLIs are the process boundary). The fake `kubectl` records its argv, copies the stdin of
the helper-pod `tar` exec to a file, and answers Job, log and `/status` queries from files the
test controls. The fake `helm` records its argv and answers `get values` and `status`.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "verify-backup-restore.sh"

SUBPROCESS_TIMEOUT_SECONDS = 60.0
INIT_LINE = "ps-postgres-init: created roles ps_state, ps_signing"
READY_STATUS_ONLY = '200 {"status":"ready","version":"0.0.0"}'
READY_TOKEN = "ready-token-value-7d1c"
READY_OK = '200 {"status":"ready","version":"0.0.0","unhealthy_dependencies":[],"gated_graphs":[]}'
READY_REPLAY = (
    '200 {"status":"not_ready","version":"0.0.0",'
    '"unhealthy_dependencies":["graph_replay"],"gated_graphs":[]}'
)
RETAINED_VALUES = {
    "psPostgres": {
        "admin": {"existingSecret": "ps-admin-retained"},
        "state": {"existingSecret": "ps-state-retained"},
        "signing": {"existingSecret": "ps-signing-retained"},
    }
}
PAYLOAD = b"not-really-a-tar-but-tar-tf-is-faked-in-the-test-by-a-real-tar"

FAKE_KUBECTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$FAKE_KUBECTL_LOG"
args="$*"
case "$args" in
  "config current-context") printf '%s\\n' "$FAKE_CONTEXT"; exit 0 ;;
  "config view --minify"*) printf 'default\\n'; exit 0 ;;
  "get secret "*)
    if [[ "$args" == *"$FAKE_MISSING_SECRET"* && -n "$FAKE_MISSING_SECRET" ]]; then exit 1; fi
    exit 0 ;;
  "get deploy/policy-system-ps-postgres "*) printf 'postgres:16-alpine\\n'; exit 0 ;;
  "get job "*) printf '%s' "$FAKE_JOB_COMPLETE"; exit 0 ;;
  "logs deploy/policy-system-ps-postgres"*) cat "$FAKE_PG_LOGS"; exit 0 ;;
  "apply -f -") cat >"$FAKE_APPLY_FILE"; exit 0 ;;
  *"exec -i"*"tar -xf"*) cat >"$FAKE_STDIN_FILE"; exit 0 ;;
  *"exec"*"deploy/policy-system-ps-service -- python -c"*)
    cat >"$FAKE_READY_STDIN"
    first="$(head -n 1 "$FAKE_READY_SEQUENCE")"
    if [[ "$(wc -l <"$FAKE_READY_SEQUENCE")" -gt 1 ]]; then
      tail -n +2 "$FAKE_READY_SEQUENCE" >"$FAKE_READY_SEQUENCE.next"
      mv "$FAKE_READY_SEQUENCE.next" "$FAKE_READY_SEQUENCE"
    fi
    printf '%s\\n' "$first"; exit 0 ;;
  *psql*)
    [[ "$args" =~ -d\\ ([a-z_]+) ]]
    cat "$FAKE_COUNTS_DIR/${BASH_REMATCH[1]}"; exit 0 ;;
esac
exit 0
"""

FAKE_HELM = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$FAKE_HELM_LOG"
case "$*" in
  "get values"*) cat "$FAKE_HELM_VALUES"; exit 0 ;;
  "status"*) printf '{"version": %s}\\n' "$FAKE_REVISION"; exit 0 ;;
  "upgrade"*) exit "${FAKE_HELM_UPGRADE_EXIT:-0}" ;;
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
    stdin_copy: Path
    manifest: Path
    ready_stdin: str


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _make_artifact(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "PG_VERSION").write_bytes(PAYLOAD)
    artifact = tmp_path / "ps-postgres-test.tar"
    tar = shutil.which("tar")
    assert tar is not None
    subprocess.run(  # noqa: S603 - tar is a shutil.which-resolved absolute path; args are literals
        [tar, "cf", str(artifact), "-C", str(source), "."], check=True, timeout=30
    )
    return artifact


def _run(
    tmp_path: Path,
    args: list[str],
    *,
    context: str = "kind-policy-system",
    stdin: str = "yes\n",
    helm_values: dict[str, object] | None = None,
    job_complete: str = "True",
    pg_logs: str = "database system is ready to accept connections\n",
    ready_lines: tuple[str, ...] = (READY_REPLAY, READY_OK),
    missing_secret: str = "",
    helm_upgrade_exit: int = 0,
    ready_token: str | None = None,
) -> RunResult:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "kubectl", FAKE_KUBECTL)
    _write_executable(bin_dir / "helm", FAKE_HELM)
    kubectl_log = tmp_path / "kubectl.log"
    kubectl_log.write_text("")
    helm_log = tmp_path / "helm.log"
    helm_log.write_text("")
    values_file = tmp_path / "values.json"
    values_file.write_text(json.dumps(RETAINED_VALUES if helm_values is None else helm_values))
    pg_logs_file = tmp_path / "pg.log"
    pg_logs_file.write_text(pg_logs)
    ready_stdin = tmp_path / "ready.stdin"
    ready_stdin.write_text("")
    ready_file = tmp_path / "ready.seq"
    ready_file.write_text("\n".join(ready_lines) + "\n")
    counts_dir = tmp_path / "counts"
    counts_dir.mkdir()
    (counts_dir / "ps_state").write_text("audit_log.audit_events=7\npublic.runtime_config=2\n")
    (counts_dir / "ps_signing").write_text("public.signing_credentials=3\n")
    stdin_copy = tmp_path / "helper-stdin.tar"
    manifest = tmp_path / "helper-pod.yaml"

    bash = shutil.which("bash")
    assert bash is not None
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CONTEXT": context,
        "FAKE_KUBECTL_LOG": str(kubectl_log),
        "FAKE_HELM_LOG": str(helm_log),
        "FAKE_HELM_VALUES": str(values_file),
        "FAKE_REVISION": "8",
        "FAKE_JOB_COMPLETE": job_complete,
        "FAKE_PG_LOGS": str(pg_logs_file),
        "FAKE_READY_SEQUENCE": str(ready_file),
        "FAKE_READY_STDIN": str(ready_stdin),
        "FAKE_COUNTS_DIR": str(counts_dir),
        "FAKE_MISSING_SECRET": missing_secret,
        "FAKE_STDIN_FILE": str(stdin_copy),
        "FAKE_APPLY_FILE": str(manifest),
        "FAKE_HELM_UPGRADE_EXIT": str(helm_upgrade_exit),
    }
    env.pop("PS_READY_TOKEN", None)
    if ready_token is not None:
        env["PS_READY_TOKEN"] = ready_token
    result = subprocess.run(  # noqa: S603 - bash is a shutil.which-resolved absolute path; args are literals
        [bash, str(SCRIPT), *args],
        env=env,
        input=stdin,
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
        helm_log.read_text().splitlines(),
        stdin_copy,
        manifest,
        ready_stdin.read_text(),
    )


def _restore_args(artifact: Path, *extra: str) -> list[str]:
    return ["restore", "--artifact", str(artifact), "--interval", "0.05", *extra]


def _index(calls: list[str], needle: str) -> int:
    for position, call in enumerate(calls):
        if needle in call:
            return position
    message = f"no kubectl call contains {needle!r}: {calls}"
    raise AssertionError(message)


def _last_index(calls: list[str], needle: str) -> int:
    matches = [position for position, call in enumerate(calls) if needle in call]
    assert matches, f"no kubectl call contains {needle!r}: {calls}"
    return matches[-1]


def test_restore_runs_the_documented_sequence_in_order(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)

    result = _run(tmp_path, _restore_args(artifact))

    assert result.code == 0, result.stderr
    calls = result.kubectl
    order = [
        _index(calls, "scale deploy/policy-system-ps-service --replicas=0"),
        _index(calls, "scale deploy/policy-system-falkordb --replicas=0"),
        _index(calls, "scale deploy/policy-system-ps-postgres --replicas=0"),
        _index(calls, "apply -f -"),
        _index(calls, "exec -i policy-system-restore-helper"),
        _index(calls, "delete pod policy-system-restore-helper"),
        _index(calls, "scale deploy/policy-system-ps-postgres --replicas=1"),
        _index(calls, "scale deploy/policy-system-falkordb --replicas=1"),
        _index(calls, "get job policy-system-ps-state-provision-r8"),
        _last_index(calls, "scale deploy/policy-system-ps-service --replicas=1"),
    ]
    assert order == sorted(order)
    assert "event=restore_complete" in result.stderr


def test_restore_streams_the_artifact_into_pgdata_unchanged(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)

    result = _run(tmp_path, _restore_args(artifact))

    assert result.code == 0, result.stderr
    assert result.stdin_copy.read_bytes() == artifact.read_bytes()
    extract = next(call for call in result.kubectl if "tar -xf" in call)
    assert "rm -rf /pg/pgdata" in extract
    assert "-C /pg/pgdata" in extract


def test_restore_empties_falkordb_through_the_helper_pod(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)

    result = _run(tmp_path, _restore_args(artifact))

    assert result.code == 0, result.stderr
    wipe = next(call for call in result.kubectl if "/fdb" in call and "rm -rf" in call)
    assert wipe.startswith("exec policy-system-restore-helper")


def test_restore_helper_mounts_both_volumes_with_the_postgres_image(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)

    result = _run(tmp_path, _restore_args(artifact))

    assert result.code == 0, result.stderr
    manifest = result.manifest.read_text()
    assert "claimName: policy-system-ps-postgres-data" in manifest
    assert "claimName: policy-system-falkordb-data" in manifest
    assert "image: postgres:16-alpine" in manifest


def test_restore_runs_helm_upgrade_with_reuse_values_and_wait_for_jobs(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)

    result = _run(tmp_path, _restore_args(artifact, "--chart", "./charts/policy-system"))

    assert result.code == 0, result.stderr
    upgrades = [call for call in result.helm if call.startswith("upgrade")]
    assert len(upgrades) == 1
    for flag in (
        "--reset-then-reuse-values",
        "--wait",
        "--wait-for-jobs",
        "./charts/policy-system",
    ):
        assert flag in upgrades[0]


def test_restore_passes_the_chart_version_to_helm(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)

    result = _run(tmp_path, _restore_args(artifact, "--chart-version", "3.23.9"))

    assert result.code == 0, result.stderr
    upgrade = next(call for call in result.helm if call.startswith("upgrade"))
    assert "--version 3.23.9" in upgrade


def test_restore_refuses_a_non_kind_context_without_allow_context(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)

    result = _run(tmp_path, _restore_args(artifact), context="aks-prod")

    assert result.code == 1
    assert "aks-prod" in result.stderr
    assert not [call for call in result.kubectl if call.startswith("scale")]


def test_restore_refuses_without_the_typed_confirmation(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)

    result = _run(tmp_path, _restore_args(artifact), stdin="no\n")

    assert result.code == 1
    assert not [call for call in result.kubectl if call.startswith("scale")]
    assert not result.helm or all(not call.startswith("upgrade") for call in result.helm)


def test_restore_yes_flag_skips_the_confirmation(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)

    result = _run(tmp_path, _restore_args(artifact, "--yes"), stdin="")

    assert result.code == 0, result.stderr


@pytest.mark.parametrize("role", ["admin", "state", "signing"])
def test_restore_refuses_when_a_credentials_secret_is_not_retained(
    tmp_path: Path, role: str
) -> None:
    artifact = _make_artifact(tmp_path)
    values: dict[str, object] = json.loads(json.dumps(RETAINED_VALUES))
    postgres = values["psPostgres"]
    assert isinstance(postgres, dict)
    postgres[role] = {}

    result = _run(tmp_path, _restore_args(artifact), helm_values=values)

    assert result.code == 1
    assert f"psPostgres.{role}.existingSecret" in result.stderr
    assert not [call for call in result.kubectl if call.startswith("scale")]


def test_restore_refuses_when_a_retained_secret_does_not_exist(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)

    result = _run(tmp_path, _restore_args(artifact), missing_secret="ps-state-retained")

    assert result.code == 1
    assert "ps-state-retained" in result.stderr
    assert not [call for call in result.kubectl if call.startswith("scale")]


def test_restore_refuses_an_unreadable_artifact_before_touching_the_cluster(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "broken.tar"
    broken.write_bytes(b"not a tar" * 100)

    result = _run(tmp_path, _restore_args(broken))

    assert result.code == 1
    assert "not a readable tar" in result.stderr
    assert not [call for call in result.kubectl if call.startswith("scale")]


def test_restore_requires_an_artifact(tmp_path: Path) -> None:
    result = _run(tmp_path, ["restore"])

    assert result.code == 1
    assert "--artifact" in result.stderr


def test_restore_fails_and_keeps_ps_service_stopped_when_the_job_is_not_complete(
    tmp_path: Path,
) -> None:
    artifact = _make_artifact(tmp_path)

    result = _run(tmp_path, _restore_args(artifact), job_complete="")

    assert result.code == 1
    assert "event=provision_failed" in result.stderr
    assert "ps-state-provision-r8" in result.stderr
    assert not [
        call
        for call in result.kubectl
        if "scale deploy/policy-system-ps-service --replicas=1" in call
    ]


def test_restore_fails_when_the_postgres_init_script_ran(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)

    result = _run(tmp_path, _restore_args(artifact), pg_logs=f"{INIT_LINE}\n")

    assert result.code == 1
    assert "event=provision_failed" in result.stderr
    assert "ps-postgres-init" in result.stderr


def test_restore_fails_when_helm_upgrade_fails(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)

    result = _run(tmp_path, _restore_args(artifact), helm_upgrade_exit=1)

    assert result.code == 1
    assert "event=restore_failed" in result.stderr
    assert not [
        call
        for call in result.kubectl
        if "scale deploy/policy-system-ps-service --replicas=1" in call
    ]


def test_restore_logs_the_counts_of_every_table_after_the_restore(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)

    result = _run(tmp_path, _restore_args(artifact))

    assert result.code == 0, result.stderr
    assert (
        "event=counts phase=restored ps_signing.public.signing_credentials=3 "
        "ps_state.audit_log.audit_events=7 ps_state.public.runtime_config=2\n"
    ) in result.stderr


def test_verify_provision_passes_for_a_complete_job_and_clean_logs(tmp_path: Path) -> None:
    result = _run(tmp_path, ["verify-provision"])

    assert result.code == 0, result.stderr
    assert "event=provision_verified job=policy-system-ps-state-provision-r8" in result.stderr


def test_verify_provision_fails_for_an_incomplete_job(tmp_path: Path) -> None:
    result = _run(tmp_path, ["verify-provision"], job_complete="False")

    assert result.code == 1
    assert "event=provision_failed" in result.stderr


def test_verify_provision_fails_when_the_init_line_is_in_the_logs(tmp_path: Path) -> None:
    result = _run(tmp_path, ["verify-provision"], pg_logs=f"x\n{INIT_LINE}\n")

    assert result.code == 1
    assert "event=provision_failed" in result.stderr


def test_wait_ready_reads_status_from_the_status_endpoint(tmp_path: Path) -> None:
    result = _run(tmp_path, ["wait-ready", "--interval", "0.05", "--timeout", "5"])

    assert result.code == 0, result.stderr
    program = "\n".join(result.kubectl)
    assert '"http://127.0.0.1:8000/status"' in program
    assert "127.0.0.1:8000/ready" not in program


def test_wait_ready_reports_replay_then_succeeds_on_200_with_no_gated_graphs(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, ["wait-ready", "--interval", "0.05", "--timeout", "20"])

    assert result.code == 0, result.stderr
    assert "event=ready_poll http=200" in result.stderr
    assert "graph_replay" in result.stderr
    assert "event=ready http=200" in result.stderr


def test_wait_ready_fails_when_a_graph_is_gated(tmp_path: Path) -> None:
    gated = (
        '200 {"status":"ready","version":"0.0.0","unhealthy_dependencies":[],"gated_graphs":["g1"]}'
    )

    result = _run(
        tmp_path,
        ["wait-ready", "--interval", "0.05", "--timeout", "2"],
        ready_lines=(gated,),
        ready_token=READY_TOKEN,
    )

    assert result.code == 1
    assert "event=ready_timeout" in result.stderr


def test_wait_ready_without_a_token_succeeds_on_a_status_only_200_and_logs_unchecked(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        ["wait-ready", "--interval", "0.05", "--timeout", "5"],
        ready_lines=(READY_STATUS_ONLY,),
    )

    assert result.code == 0, result.stderr
    assert "event=ready http=200" in result.stderr
    assert "gated_graphs=unchecked" in result.stderr
    assert result.ready_stdin == ""


def test_wait_ready_without_a_token_does_not_accept_a_not_ready_status(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        ["wait-ready", "--interval", "0.05", "--timeout", "1"],
        ready_lines=('200 {"status":"not_ready","version":"0.0.0"}',),
    )

    assert result.code == 1
    assert "event=ready_timeout" in result.stderr


def test_wait_ready_with_a_token_sends_it_to_the_pod_and_checks_gated_graphs(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        ["wait-ready", "--interval", "0.05", "--timeout", "5"],
        ready_lines=(READY_OK,),
        ready_token=READY_TOKEN,
    )

    assert result.code == 0, result.stderr
    assert result.ready_stdin == READY_TOKEN
    assert "gated_graphs=unchecked" not in result.stderr
    assert "Authorization" in SCRIPT.read_text()


def test_wait_ready_with_a_token_fails_when_the_reply_carries_no_gated_graphs_field(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        ["wait-ready", "--interval", "0.05", "--timeout", "1"],
        ready_lines=(READY_STATUS_ONLY,),
        ready_token=READY_TOKEN,
    )

    assert result.code == 1
    assert "event=ready_timeout" in result.stderr


@pytest.mark.parametrize(
    "ready_lines",
    [(READY_OK,), ('200 {"status":"ready","version":"0.0.0","gated_graphs":["g"]}',)],
)
def test_the_ready_token_never_reaches_a_log_line_or_a_kubectl_argument(
    tmp_path: Path, ready_lines: tuple[str, ...]
) -> None:
    result = _run(
        tmp_path,
        ["wait-ready", "--interval", "0.05", "--timeout", "1"],
        ready_lines=ready_lines,
        ready_token=READY_TOKEN,
    )

    assert READY_TOKEN not in result.stderr
    assert READY_TOKEN not in result.stdout
    assert all(READY_TOKEN not in call for call in result.kubectl)


def test_wait_ready_times_out_while_the_service_stays_not_ready(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        ["wait-ready", "--interval", "0.05", "--timeout", "1"],
        ready_lines=(READY_REPLAY,),
    )

    assert result.code == 1
    assert "event=ready_timeout" in result.stderr


@pytest.mark.parametrize(
    "flag",
    [
        "--reset-then-reuse-values",
        "--wait-for-jobs",
        "ps-postgres-init: created roles",
        "gated_graphs",
    ],
)
def test_script_carries_the_documented_restore_strings(flag: str) -> None:
    assert flag in SCRIPT.read_text()


def test_retain_secrets_copies_the_secrets_then_stops_postgres_then_upgrades(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, ["retain-secrets"], helm_values={})

    assert result.code == 0, result.stderr
    applies = [call for call in result.kubectl if call == "apply -f -"]
    assert len(applies) == 3
    for role in ("admin", "state", "signing"):
        assert f"get secret policy-system-ps-postgres-{role}-credentials -o json" in result.kubectl
    scale = _index(result.kubectl, "scale deploy/policy-system-ps-service")
    assert "deploy/policy-system-ps-postgres --replicas=0" in result.kubectl[scale]
    assert scale > _last_index(result.kubectl, "apply -f -")
    assert (
        _index(result.kubectl, "wait --for=delete pod -l app.kubernetes.io/component=ps-postgres")
        > scale
    )
    upgrade = next(call for call in result.helm if call.startswith("upgrade"))
    assert "--reset-then-reuse-values" in upgrade
    assert "--wait --wait-for-jobs" in upgrade
    for role in ("admin", "state", "signing"):
        assert f"--set psPostgres.{role}.existingSecret=ps-{role}-retained" in upgrade


def test_retain_secrets_passes_the_chart_version_to_helm(tmp_path: Path) -> None:
    result = _run(tmp_path, ["retain-secrets", "--chart-version", "5.0.0"], helm_values={})

    assert result.code == 0, result.stderr
    upgrade = next(call for call in result.helm if call.startswith("upgrade"))
    assert "--version 5.0.0" in upgrade


def test_retain_secrets_does_nothing_when_the_release_already_retains_them(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, ["retain-secrets"])

    assert result.code == 0, result.stderr
    assert "retain_secrets_skipped" in result.stderr
    assert not [call for call in result.kubectl if call.startswith(("apply", "scale"))]
    assert not [call for call in result.helm if call.startswith("upgrade")]


def test_retain_secrets_refuses_outside_a_kind_context(tmp_path: Path) -> None:
    result = _run(tmp_path, ["retain-secrets"], context="aks-prod", helm_values={})

    assert result.code == 1
    assert "aks-prod" in result.stderr
    assert not [call for call in result.kubectl if call.startswith(("apply", "scale"))]


def test_retain_secrets_fails_when_helm_upgrade_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, ["retain-secrets"], helm_values={}, helm_upgrade_exit=1)

    assert result.code == 1
    assert "retain-secrets failed at upgrade" in result.stderr


def test_restore_refusal_points_to_retain_secrets(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)

    result = _run(tmp_path, _restore_args(artifact), helm_values={"psPostgres": {}})

    assert result.code == 1
    assert "retain-secrets" in result.stderr
