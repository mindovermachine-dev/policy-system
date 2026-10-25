"""Tests for `scripts/verify-backup-restore.sh seed | e2e`.

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
import re
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
COUNTS = {
    "ps_state": [
        "audit_log.audit_events=7",
        "graph_log.entries=5",
        "public.access_role_assignments=2",
        "public.ps_schema_migrations=4",
    ],
    "ps_signing": ["public.pending_approvals=1", "public.signing_credentials=1"],
}
WIPED_COUNTS = {
    "ps_state": [
        "audit_log.audit_events=0",
        "graph_log.entries=0",
        "public.access_role_assignments=0",
        "public.ps_schema_migrations=4",
    ],
    "ps_signing": ["public.pending_approvals=0", "public.signing_credentials=0"],
}
COUNTS_LINE = (
    "ps_signing.public.pending_approvals=1 ps_signing.public.signing_credentials=1 "
    "ps_state.audit_log.audit_events=7 ps_state.graph_log.entries=5 "
    "ps_state.public.access_role_assignments=2 ps_state.public.ps_schema_migrations=4"
)

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
  *"exec"*"deploy/policy-system-ps-service -- python -c"*)
    cat >/dev/null; printf '%s\\n' "$FAKE_READY"; exit 0 ;;
  *"exec -i deploy/policy-system-ps-service -- python -"*)
    cat >>"$FAKE_DIR/seed-stdin.txt"; exit 0 ;;
  *"psql"*"DISTINCT graph"*) printf 'alpha\\nbeta\\n'; exit 0 ;;
  *psql*)
    [[ "$args" =~ -d\\ ([a-z_]+) ]]
    phase=before
    [[ -e "$FAKE_DIR/helper-applied" ]] && phase=wiped
    [[ -e "$FAKE_DIR/restored" ]] && phase=after
    fail_var="FAKE_PSQL_FAIL_${BASH_REMATCH[1]}"
    if [[ -n "${!fail_var:-}" ]]; then exit "${!fail_var}"; fi
    cat "$FAKE_DIR/counts-$phase/${BASH_REMATCH[1]}"
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
    counts_before: dict[str, list[str]] | None = None,
    counts_after: dict[str, list[str]] | None = None,
    psql_failures: dict[str, int] | None = None,
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
    for phase, counts in (
        ("before", counts_before),
        ("after", counts_after),
        ("wiped", WIPED_COUNTS),
    ):
        (work / f"counts-{phase}").mkdir()
        for database, entries in (COUNTS if counts is None else counts).items():
            (work / f"counts-{phase}" / database).write_text(
                "".join(f"{entry}\n" for entry in entries)
            )
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
    }
    for database, code in (psql_failures or {}).items():
        env[f"FAKE_PSQL_FAIL_{database}"] = str(code)
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


def test_seed_pipes_the_three_seed_tools_into_the_service_pod_with_the_instruments(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, ["seed", "--instrument", "A-1.0", "--instrument", "B-1.0"])

    assert result.code == 0, result.stderr
    content = _index(result.kubectl, "exec -i deploy/policy-system-ps-service -- python -")
    assert "--instrument A-1.0 --instrument B-1.0" in result.kubectl[content]
    grants = _index(
        result.kubectl, "exec -i deploy/policy-system-ps-service -- python -", start=content + 1
    )
    state = _index(
        result.kubectl, "exec -i deploy/policy-system-ps-service -- python -", start=grants + 1
    )
    assert content < grants < state
    assert result.seed_stdin == (
        (TOOLS / "seed_content.py").read_text()
        + (TOOLS / "seed_grants.py").read_text()
        + (TOOLS / "seed_state.py").read_text()
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
    changed = {
        **COUNTS,
        "ps_signing": ["public.pending_approvals=1", "public.signing_credentials=0"],
    }
    result = _run(tmp_path, _e2e_args(tmp_path), counts_after=changed)

    assert result.code == 1
    assert "event=e2e_verified" not in result.stderr
    assert (
        "Count differs for ps_signing.public.signing_credentials: before 1, after 0"
        in result.stderr
    )
    assert "Count differs for ps_signing.public.pending_approvals" not in result.stderr


def test_e2e_names_a_table_that_is_missing_after_the_restore(tmp_path: Path) -> None:
    missing = {**COUNTS, "ps_signing": ["public.pending_approvals=1"]}
    result = _run(tmp_path, _e2e_args(tmp_path), counts_after=missing)

    assert result.code == 1
    assert (
        "Count differs for ps_signing.public.signing_credentials: before 1, after absent"
        in result.stderr
    )
    assert "event=e2e_verified" not in result.stderr


def test_e2e_names_a_table_that_only_exists_after_the_restore(tmp_path: Path) -> None:
    extra = {**COUNTS, "ps_state": [*COUNTS["ps_state"], "public.extra_table=3"]}
    result = _run(tmp_path, _e2e_args(tmp_path), counts_after=extra)

    assert result.code == 1
    assert "Count differs for ps_state.public.extra_table: before absent, after 3" in result.stderr


def test_e2e_logs_identical_before_and_after_counts_for_both_databases(tmp_path: Path) -> None:
    result = _run(tmp_path, _e2e_args(tmp_path))

    assert result.code == 0, result.stderr
    assert f"event=counts phase=before {COUNTS_LINE}\n" in result.stderr
    assert f"event=counts phase=after {COUNTS_LINE}\n" in result.stderr


def test_seed_logs_the_counts_of_every_table(tmp_path: Path) -> None:
    result = _run(tmp_path, ["seed"])

    assert result.code == 0, result.stderr
    assert f"event=counts phase=seeded {COUNTS_LINE}\n" in result.stderr


def test_e2e_stops_before_the_backup_when_the_counts_cannot_be_read(tmp_path: Path) -> None:
    result = _run(tmp_path, _e2e_args(tmp_path), psql_failures={"ps_signing": 2})

    assert result.code == 1
    assert "event=e2e_verified" not in result.stderr
    assert not any("pg_basebackup" in call for call in result.kubectl)


def test_e2e_skip_restore_leaves_the_data_unrestored_and_fails_the_comparison(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, _e2e_args(tmp_path, "--skip-restore"))

    assert result.code == 1
    assert not any("tar -xf" in call for call in result.kubectl)
    assert "graph alpha" in result.stderr
    assert "Count differs for ps_state.audit_log.audit_events: before 7, after 0" in result.stderr
    assert "Count differs for ps_state.public.ps_schema_migrations" not in result.stderr


EMPTY_SIGNING = {
    **COUNTS,
    "ps_signing": ["public.pending_approvals=0", "public.signing_credentials=1"],
}


def test_e2e_stops_before_the_backup_when_a_table_is_empty_after_seeding(tmp_path: Path) -> None:
    result = _run(tmp_path, _e2e_args(tmp_path), counts_before=EMPTY_SIGNING)

    assert result.code == 1
    assert "ps_signing.public.pending_approvals" in result.stderr
    assert "event=e2e_failed" in result.stderr
    assert "event=e2e_verified" not in result.stderr
    assert not any("pg_basebackup" in call for call in result.kubectl)


def test_seed_fails_and_names_every_table_that_is_empty_after_seeding(tmp_path: Path) -> None:
    both_empty = {
        "ps_state": [*COUNTS["ps_state"][:-1], "public.ps_schema_migrations=0"],
        "ps_signing": EMPTY_SIGNING["ps_signing"],
    }
    result = _run(tmp_path, ["seed"], counts_before=both_empty)

    assert result.code == 1
    assert "ps_signing.public.pending_approvals" in result.stderr
    assert "ps_state.public.ps_schema_migrations" in result.stderr
    assert 'Table "ps_state.graph_log.entries" is empty' not in result.stderr


def _source(tmp_path: Path, snippet: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(  # noqa: S603 - bash is a shutil.which-resolved absolute path; args are literals
        [bash, "-c", f'source "$1"; shift; {snippet}', "_", str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
        cwd=tmp_path,
    )


def test_seed_guard_passes_an_empty_table_named_in_the_exception_list(tmp_path: Path) -> None:
    line = "ps_signing.public.pending_approvals=0 ps_state.graph_log.entries=5"

    exempt = _source(
        tmp_path,
        'assert_seeded "$@"',
        line,
        "ps_signing.public.pending_approvals|the live seed leaves it empty",
    )
    unlisted = _source(tmp_path, 'assert_seeded "$@"', line)

    assert exempt.returncode == 0, exempt.stderr
    assert unlisted.returncode == 1
    assert "ps_signing.public.pending_approvals" in unlisted.stderr


def test_seed_guard_does_not_exempt_a_table_that_only_shares_a_prefix(tmp_path: Path) -> None:
    result = _source(
        tmp_path,
        'assert_seeded "$@"',
        "ps_state.public.runtime_config_extra=0",
        "ps_state.public.runtime_config|reason",
    )

    assert result.returncode == 1
    assert "ps_state.public.runtime_config_extra" in result.stderr


def test_seed_empty_exceptions_are_declared_and_each_carries_a_reason(tmp_path: Path) -> None:
    result = _source(
        tmp_path,
        'for entry in ${SEED_EMPTY_EXCEPTIONS[@]+"${SEED_EMPTY_EXCEPTIONS[@]}"}; do '
        'printf "%s\\n" "$entry"; done',
    )

    assert result.returncode == 0, result.stderr
    for entry in result.stdout.splitlines():
        assert re.fullmatch(r"[a-z_]+\.[a-z_]+\.[a-z_]+\|\S.*", entry), entry


COUNTS_BEFORE_LINE = "ps_state.graph_log.entries=5 ps_state.public.runtime_config=1"


def test_count_comparison_passes_when_every_entry_is_equal(tmp_path: Path) -> None:
    result = _source(tmp_path, 'compare_counts "$@"', COUNTS_BEFORE_LINE, COUNTS_BEFORE_LINE)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_count_comparison_skips_a_volatile_table_and_logs_it(tmp_path: Path) -> None:
    after = "ps_state.graph_log.entries=9 ps_state.public.runtime_config=1"
    volatile = "ps_state.graph_log.entries|rewritten by replay at startup"

    skipped = _source(tmp_path, 'compare_counts "$@"', COUNTS_BEFORE_LINE, after, volatile)
    listed_only = _source(tmp_path, 'compare_counts "$@"', COUNTS_BEFORE_LINE, after)

    assert skipped.returncode == 0, skipped.stderr
    assert "event=counts_volatile_skipped table=ps_state.graph_log.entries" in skipped.stderr
    assert "Count differs" not in skipped.stderr
    assert listed_only.returncode == 1
    assert "Count differs for ps_state.graph_log.entries: before 5, after 9" in listed_only.stderr


def test_count_comparison_still_reports_the_other_tables_when_one_is_volatile(
    tmp_path: Path,
) -> None:
    after = "ps_state.graph_log.entries=9 ps_state.public.runtime_config=0"
    volatile = "ps_state.graph_log.entries|rewritten by replay at startup"

    result = _source(tmp_path, 'compare_counts "$@"', COUNTS_BEFORE_LINE, after, volatile)

    assert result.returncode == 1
    assert "Count differs for ps_state.public.runtime_config: before 1, after 0" in result.stderr
    assert "Count differs for ps_state.graph_log.entries" not in result.stderr


def test_count_comparison_does_not_treat_a_name_prefix_as_the_same_table(tmp_path: Path) -> None:
    result = _source(
        tmp_path,
        'compare_counts "$@"',
        "ps_state.public.runtime_config=1",
        "ps_state.public.runtime_config=1 ps_state.public.runtime_config_extra=2",
        "ps_state.public.runtime_config|reason",
    )

    assert result.returncode == 1
    assert "Count differs for ps_state.public.runtime_config_extra" in result.stderr


def test_sourcing_the_script_declares_the_lists_and_runs_nothing(tmp_path: Path) -> None:
    result = _source(tmp_path, "declare -p SEED_EMPTY_EXCEPTIONS VOLATILE_COUNT_TABLES")

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("declare -") == 2
    assert "event=" not in result.stderr
    assert "Usage" not in result.stderr


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
