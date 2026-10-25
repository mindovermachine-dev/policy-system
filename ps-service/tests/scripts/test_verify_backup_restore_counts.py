"""Tests for the catalog-driven row counts of `scripts/verify-backup-restore.sh`.

The script runs for real against a fake `kubectl` on PATH (the external CLI is the process
boundary). The fake answers each `psql` call from the fixture file named after the `-d` database
and records the SQL it was handed, so the tests see both what the script prints and what it asks
the database.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "verify-backup-restore.sh"

SUBPROCESS_TIMEOUT_SECONDS = 30.0

STATE_ROWS = [
    "public.runtime_config=1",
    "audit_log.audit_events=7",
    "public.ps_schema_migrations=5",
    "graph_log.entries=12",
]
SIGNING_ROWS = [
    "public.signing_credentials=2",
    "public.pending_approvals=1",
    "public.schema_migrations=2",
]

FAKE_KUBECTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$FAKE_DIR/kubectl.log"
if [[ "$1 $2" == "config current-context" ]]; then
  printf '%s\\n' "$FAKE_CONTEXT"
  exit 0
fi
args="$*"
if [[ "$args" == *psql* ]]; then
  [[ "$args" =~ -d\\ ([a-z_]+) ]]
  db="${BASH_REMATCH[1]}"
  sql=""
  previous=""
  for argument in "$@"; do
    [[ "$previous" == "-c" ]] && sql="$argument"
    previous="$argument"
  done
  printf '%s' "$sql" >"$FAKE_DIR/sql-$db"
  fail_var="FAKE_PSQL_FAIL_$db"
  if [[ -n "${!fail_var:-}" ]]; then
    printf 'psql: error: connection to server failed\\n' >&2
    exit "${!fail_var}"
  fi
  cat "$FAKE_DIR/counts/$db"
  exit 0
fi
exit 99
"""


@dataclass(frozen=True)
class RunResult:
    code: int
    stdout: str
    stderr: str
    kubectl_log: str
    sql: dict[str, str]


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run(
    tmp_path: Path,
    args: list[str],
    *,
    state_rows: list[str] | None = None,
    signing_rows: list[str] | None = None,
    psql_failures: dict[str, int] | None = None,
) -> RunResult:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "kubectl", FAKE_KUBECTL)
    work = tmp_path / "work"
    (work / "counts").mkdir(parents=True)
    rows = {
        "ps_state": STATE_ROWS if state_rows is None else state_rows,
        "ps_signing": SIGNING_ROWS if signing_rows is None else signing_rows,
    }
    for database, entries in rows.items():
        (work / "counts" / database).write_text("".join(f"{entry}\n" for entry in entries))
    bash = shutil.which("bash")
    assert bash is not None
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DIR": str(work),
        "FAKE_CONTEXT": "kind-policy-system",
    }
    for database, code in (psql_failures or {}).items():
        env[f"FAKE_PSQL_FAIL_{database}"] = str(code)
    result = subprocess.run(  # noqa: S603 - bash is a shutil.which-resolved absolute path; args are literals
        [bash, str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
        cwd=tmp_path,
    )
    sql = {path.name.removeprefix("sql-"): path.read_text() for path in sorted(work.glob("sql-*"))}
    return RunResult(
        result.returncode,
        result.stdout,
        result.stderr,
        (work / "kubectl.log").read_text(),
        sql,
    )


def test_counts_prints_one_sorted_entry_per_table_of_both_databases(tmp_path: Path) -> None:
    result = _run(tmp_path, ["counts"])

    assert result.code == 0, result.stderr
    assert result.stdout == (
        "ps_signing.public.pending_approvals=1 "
        "ps_signing.public.schema_migrations=2 "
        "ps_signing.public.signing_credentials=2 "
        "ps_state.audit_log.audit_events=7 "
        "ps_state.graph_log.entries=12 "
        "ps_state.public.ps_schema_migrations=5 "
        "ps_state.public.runtime_config=1\n"
    )


def test_counts_includes_a_table_that_only_the_catalog_knows(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        ["counts"],
        state_rows=[*STATE_ROWS, "newfeature.widgets=3"],
        signing_rows=[*SIGNING_ROWS, "public.attestations=4"],
    )

    assert result.code == 0, result.stderr
    entries = result.stdout.split()
    assert "ps_state.newfeature.widgets=3" in entries
    assert "ps_signing.public.attestations=4" in entries
    assert entries == sorted(entries)


def test_counts_queries_both_databases_once_each(tmp_path: Path) -> None:
    result = _run(tmp_path, ["counts"])

    assert result.code == 0, result.stderr
    assert set(result.sql) == {"ps_state", "ps_signing"}
    psql_calls = [line for line in result.kubectl_log.splitlines() if "psql" in line]
    assert len(psql_calls) == 2
    for call in psql_calls:
        assert "-U postgres_admin" in call
        assert "-v ON_ERROR_STOP=1" in call
        assert " -tA " in call


@pytest.mark.parametrize("failing", ["ps_state", "ps_signing"])
@pytest.mark.parametrize(
    "failure", ["connection-fails", "no-tables"], ids=["connection-fails", "no-tables"]
)
def test_counts_fails_without_output_when_one_database_cannot_be_counted(
    tmp_path: Path, failing: str, failure: str
) -> None:
    if failure == "connection-fails":
        result = _run(tmp_path, ["counts"], psql_failures={failing: 2})
    else:
        empty = {"ps_state": STATE_ROWS, "ps_signing": SIGNING_ROWS}
        empty[failing] = []
        result = _run(
            tmp_path,
            ["counts"],
            state_rows=empty["ps_state"],
            signing_rows=empty["ps_signing"],
        )

    assert result.code == 1
    assert result.stdout == ""
    assert "event=counts" not in result.stderr


@pytest.mark.parametrize("failing", ["ps_state", "ps_signing"])
def test_backup_logs_no_counts_and_takes_no_backup_when_counting_fails(
    tmp_path: Path, failing: str
) -> None:
    result = _run(
        tmp_path,
        ["backup", "--out-dir", str(tmp_path / "out")],
        psql_failures={failing: 2},
    )

    assert result.code == 1
    assert "event=counts" not in result.stderr
    assert "pg_basebackup" not in result.kubectl_log


@pytest.mark.parametrize(
    "bad_row",
    [
        "public.bad name=3",
        "public.t=x",
        "public.t=",
        "public.t=1=2",
        "public.t",
        'public."Mixed"=3',
        "t=3",
    ],
)
def test_counts_fails_closed_on_a_malformed_row(tmp_path: Path, bad_row: str) -> None:
    result = _run(tmp_path, ["counts"], state_rows=[*STATE_ROWS, bad_row])

    assert result.code == 1
    assert result.stdout == ""
    assert "event=counts" not in result.stderr


@pytest.mark.parametrize("database", ["ps_state", "ps_signing"])
def test_counting_query_only_reads_the_catalog_and_counts(tmp_path: Path, database: str) -> None:
    result = _run(tmp_path, ["counts"])
    sql = result.sql[database]

    assert re.match(r"\s*SELECT\b", sql)
    assert not re.search(
        r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|GRANT)\b", sql, re.IGNORECASE
    )
    assert "format('%I.%I'" in sql
    assert "count(*)" in sql
    assert "pg_catalog" in sql
    assert "information_schema" in sql
    assert "^pg_(toast|temp)" in sql
    assert "relkind IN ('r', 'p')" in sql
    assert "NOT c.relispartition" in sql
    assert "*" not in sql.replace("count(*)", "")


def test_script_carries_no_hard_coded_table_list() -> None:
    text = SCRIPT.read_text()

    assert "graph_log_" not in text
    assert "access_role_assignments=" not in text
    assert "count_rows" not in text


SERVICE_SOURCE = REPO_ROOT / "ps-service" / "src" / "ps_service"
_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:(\w+)\.)?(\w+)", re.IGNORECASE
)
_MOVE_TABLE = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:(\w+)\.)?(\w+)\s+SET\s+SCHEMA\s+(\w+)",
    re.IGNORECASE,
)
_BOOKKEEPING_TABLES = {
    "ps_state.public.ps_schema_migrations",
    "ps_signing.public.schema_migrations",
}


def _migration_files() -> list[Path]:
    ordinary = sorted(SERVICE_SOURCE.glob("**/migrations/*.sql"))
    privileged = sorted(SERVICE_SOURCE.glob("**/privileged_migrations/*.sql"))
    return [*ordinary, *privileged]


def _tables_created_by_migrations() -> set[str]:
    """Return `<database>.<schema>.<table>` for every table the migrations leave in place."""
    tables: set[str] = set()
    for path in _migration_files():
        database = "ps_signing" if "passkey_signing" in path.parts else "ps_state"
        text = re.sub(r"--[^\n]*", "", path.read_text())
        for statement in text.split(";"):
            if created := _CREATE_TABLE.search(statement):
                tables.add(f"{database}.{created.group(1) or 'public'}.{created.group(2)}")
            if moved := _MOVE_TABLE.search(statement):
                tables.discard(f"{database}.{moved.group(1) or 'public'}.{moved.group(2)}")
                tables.add(f"{database}.{moved.group(3)}.{moved.group(2)}")
    return tables | _BOOKKEEPING_TABLES


def _list_problems(entries: list[str], known_tables: set[str]) -> list[str]:
    """Return what is wrong with `<table>|<reason>` entries: an unknown table or no reason."""
    problems: list[str] = []
    for entry in entries:
        table, separator, reason = entry.partition("|")
        if table not in known_tables:
            problems.append(f"{table} is not a table of ps_state or ps_signing")
        if not separator or not reason.strip():
            problems.append(f"{table} has no reason")
    return problems


def _script_list(tmp_path: Path, name: str) -> list[str]:
    bash = shutil.which("bash")
    assert bash is not None
    snippet = (
        f'source "$1"; for entry in ${{{name}[@]+"${{{name}[@]}}"}}; '
        'do printf "%s\\n" "$entry"; done'
    )
    result = subprocess.run(  # noqa: S603 - bash is a shutil.which-resolved absolute path; args are literals
        [bash, "-c", snippet, "_", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_tables_created_by_the_migrations_include_the_moved_audit_table_and_both_databases() -> (
    None
):
    tables = _tables_created_by_migrations()

    assert {
        "ps_state.audit_log.audit_events",
        "ps_state.audit_log.audit_chain_head",
        "ps_state.public.runtime_config",
        "ps_state.public.ingestion_runs",
        "ps_state.public.access_role_assignments",
        "ps_state.graph_log.entries",
        "ps_signing.public.pending_approvals",
        "ps_signing.public.signing_credentials",
        "ps_state.public.ps_schema_migrations",
        "ps_signing.public.schema_migrations",
    } <= tables
    assert "ps_state.public.audit_events" not in tables


@pytest.mark.parametrize("name", ["VOLATILE_COUNT_TABLES", "SEED_EMPTY_EXCEPTIONS"])
def test_volatile_list_names_only_existing_tables(tmp_path: Path, name: str) -> None:
    entries = _script_list(tmp_path, name)

    assert _list_problems(entries, _tables_created_by_migrations()) == []


def test_a_listed_table_that_no_migration_creates_is_reported() -> None:
    known = _tables_created_by_migrations()

    assert _list_problems(["ps_state.public.no_such_table|why"], known) == [
        "ps_state.public.no_such_table is not a table of ps_state or ps_signing"
    ]
    assert _list_problems(["ps_state.graph_log.entries"], known) == [
        "ps_state.graph_log.entries has no reason"
    ]
    assert _list_problems(["ps_state.graph_log.entries|rewritten by replay"], known) == []
