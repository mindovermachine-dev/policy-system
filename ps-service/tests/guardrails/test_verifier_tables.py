"""Verifier table guard: every table the migrations create is proven by the backup-restore seed.

`scripts/verify-backup-restore.sh` counts every table of `ps_state` and `ps_signing` and fails a
round trip whose seeded cluster leaves a table empty, unless `SEED_EMPTY_EXCEPTIONS` lists it with
a reason. The seed tools in `tools/backup-restore-verify` write through the service's stores, so
their code never names a table; `guardrails/verifier_seeding.py` declares which tool fills each
one. This guard holds the declaration, the exception list and the migrations to each other: a new
table must be declared or excepted, and neither list may name a table no migration creates. The
row counts themselves are proven only by the live round trip.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from types import MappingProxyType
from typing import TYPE_CHECKING

import pytest

from guardrails.migration_registry import REGISTRY, MigrationCoverage
from guardrails.migration_sources import (
    BOOKKEEPING_TABLES,
    REPO_ROOT,
    SERVICE_SOURCE,
    migration_directories,
    tables_created_by_migrations,
)
from guardrails.verifier_seeding import SEEDED_TABLES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

SCRIPT = REPO_ROOT / "scripts" / "verify-backup-restore.sh"
TOOLS_DIR = REPO_ROOT / "tools" / "backup-restore-verify"

SUBPROCESS_TIMEOUT_SECONDS = 30.0

_PIPE_TOOL = re.compile(r"pipe_tool (seed_\w+\.py)")


def _list_problems(entries: Sequence[str], known_tables: set[str] | frozenset[str]) -> list[str]:
    """Return what is wrong with `<table>|<reason>` entries: an unknown table or no reason."""
    problems: list[str] = []
    for entry in entries:
        table, separator, reason = entry.partition("|")
        if table not in known_tables:
            problems.append(
                f"{table} is not a table of ps_state or ps_signing. "
                "How to fix: remove or correct the entry in scripts/verify-backup-restore.sh"
            )
        if not separator or not reason.strip():
            problems.append(
                f"{table} has no reason. How to fix: write the entry as `<table>|<reason>` in "
                "scripts/verify-backup-restore.sh"
            )
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


def _run_tools(script_text: str) -> set[str]:
    """Return the seed tools the verifier runs (`pipe_tool seed_<name>.py`)."""
    return set(_PIPE_TOOL.findall(script_text))


def _databases(
    entries: Sequence[MigrationCoverage], directories: Sequence[Path], repo_root: Path
) -> tuple[dict[Path, str], list[str]]:
    """Map each discovered migration directory to its registered database.

    Returns the mapping and one problem per discovered directory that has no registry entry, so
    a new directory cannot hide its tables from this guard.
    """
    registered = {(repo_root / entry.directory).resolve(): entry.database for entry in entries}
    databases: dict[Path, str] = {}
    problems: list[str] = []
    for directory in directories:
        resolved = directory.resolve()
        if resolved in registered:
            databases[resolved] = registered[resolved]
        else:
            problems.append(
                f"{directory}: has migrations but no database in guardrails/migration_registry.py."
                " How to fix: register it (migration coverage guard)"
            )
    return databases, problems


def _exception_problems(exceptions: Sequence[str], created: set[str]) -> list[str]:
    problems: list[str] = []
    for entry in exceptions:
        table = entry.partition("|")[0]
        if table in BOOKKEEPING_TABLES:
            problems.append(
                f"{table}: is a migration runner table, never empty once migrated; it cannot be "
                "a SEED_EMPTY_EXCEPTIONS entry. How to fix: remove the entry"
            )
        else:
            problems.extend(_list_problems([entry], created))
    return problems


def _declaration_problems(
    created: set[str], seeded: Mapping[str, str], run_tools: set[str], tools_dir: Path
) -> list[str]:
    problems: list[str] = []
    for table, tool in sorted(seeded.items()):
        if table in BOOKKEEPING_TABLES:
            problems.append(
                f"{table}: is a migration runner table and needs no seeder. "
                "How to fix: remove it from guardrails/verifier_seeding.py"
            )
        elif table not in created:
            problems.append(
                f"{table}: declared seeded but no migration creates it. "
                "How to fix: remove or correct the entry in guardrails/verifier_seeding.py"
            )
        if not (tools_dir / tool).is_file():
            problems.append(
                f"{table}: declared seeded by {tool}, which is not in tools/backup-restore-verify."
                " How to fix: name an existing seed tool"
            )
        elif tool not in run_tools:
            problems.append(
                f"{table}: declared seeded by {tool}, which scripts/verify-backup-restore.sh never"
                " runs. How to fix: run it with pipe_tool in the seed step, or name a tool it runs"
            )
    return problems


def _verifier_table_problems(
    created: set[str],
    seeded: Mapping[str, str],
    exceptions: Sequence[str],
    run_tools: set[str],
    tools_dir: Path,
) -> list[str]:
    """Return every way the seed declaration and the exception list fail the migrations' tables.

    A created table must be declared seeded by a tool the verifier runs, or be on
    `SEED_EMPTY_EXCEPTIONS` with a reason, and not both. A declaration or exception must name a
    table a migration creates. The migration runners' tables (`BOOKKEEPING_TABLES`) belong on
    neither list: applying any migration writes a row in them, so they are never empty on a
    migrated database and excepting or seeding them means nothing.
    """
    excepted = {entry.partition("|")[0] for entry in exceptions}
    problems = [
        f"{table}: created by a migration but neither seeded by a tool in "
        "tools/backup-restore-verify nor on SEED_EMPTY_EXCEPTIONS in "
        "scripts/verify-backup-restore.sh. How to fix: declare its seed tool in "
        "guardrails/verifier_seeding.py, or add `<table>|<reason>` to SEED_EMPTY_EXCEPTIONS"
        for table in sorted(created)
        if table not in seeded and table not in excepted
    ]
    problems.extend(_declaration_problems(created, seeded, run_tools, tools_dir))
    problems.extend(_exception_problems(exceptions, created))
    problems.extend(
        f"{table}: is both declared seeded and on SEED_EMPTY_EXCEPTIONS. "
        "How to fix: keep only one of them"
        for table in sorted(excepted & seeded.keys())
    )
    return problems


def _real_databases() -> dict[Path, str]:
    databases, problems = _databases(REGISTRY, migration_directories(SERVICE_SOURCE), REPO_ROOT)
    assert problems == [], "\n".join(problems)
    return databases


# --- synthetic input -------------------------------------------------------------------------

_WIDGETS = "ps_state.public.widgets"
_TOOL = "seed_widgets.py"


@pytest.fixture
def tools_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "tools"
    directory.mkdir()
    (directory / _TOOL).write_text("")
    return directory


def _check(
    tools_dir: Path,
    *,
    created: set[str] | None = None,
    seeded: Mapping[str, str] | None = None,
    exceptions: Sequence[str] = (),
    run_tools: set[str] | None = None,
) -> list[str]:
    return _verifier_table_problems(
        created={_WIDGETS} if created is None else created,
        seeded=MappingProxyType({_WIDGETS: _TOOL}) if seeded is None else seeded,
        exceptions=exceptions,
        run_tools={_TOOL} if run_tools is None else run_tools,
        tools_dir=tools_dir,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_a_seeded_table_made_by_a_tool_the_verifier_runs_has_no_problems(tools_dir: Path) -> None:
    assert _check(tools_dir) == []


def test_a_migration_table_neither_seeded_nor_excepted_is_reported(tools_dir: Path) -> None:
    assert _check(tools_dir, seeded={}) == [
        (
            "ps_state.public.widgets: created by a migration but neither seeded by a tool in "
            "tools/backup-restore-verify nor on SEED_EMPTY_EXCEPTIONS in "
            "scripts/verify-backup-restore.sh. How to fix: declare its seed tool in "
            "guardrails/verifier_seeding.py, or add `<table>|<reason>` to SEED_EMPTY_EXCEPTIONS"
        )
    ]


def test_a_table_on_the_exception_list_needs_no_seeder(tools_dir: Path) -> None:
    assert _check(tools_dir, seeded={}, exceptions=[f"{_WIDGETS}|filled only on first use"]) == []


def test_a_seeded_declaration_for_a_table_no_migration_creates_is_reported(
    tools_dir: Path,
) -> None:
    assert _check(tools_dir, created=set()) == [
        (
            "ps_state.public.widgets: declared seeded but no migration creates it. "
            "How to fix: remove or correct the entry in guardrails/verifier_seeding.py"
        )
    ]


def test_an_exception_for_a_table_no_migration_creates_is_reported(tools_dir: Path) -> None:
    exceptions = ["ps_state.public.no_such_table|filled only on first use"]

    assert _check(tools_dir, exceptions=exceptions) == [
        (
            "ps_state.public.no_such_table is not a table of ps_state or ps_signing. "
            "How to fix: remove or correct the entry in scripts/verify-backup-restore.sh"
        )
    ]


def test_a_table_both_seeded_and_excepted_is_reported(tools_dir: Path) -> None:
    assert _check(tools_dir, exceptions=[f"{_WIDGETS}|filled only on first use"]) == [
        (
            "ps_state.public.widgets: is both declared seeded and on SEED_EMPTY_EXCEPTIONS. "
            "How to fix: keep only one of them"
        )
    ]


def test_a_seeding_tool_the_verifier_does_not_run_is_reported(tools_dir: Path) -> None:
    assert _check(tools_dir, run_tools={"seed_other.py"}) == [
        (
            "ps_state.public.widgets: declared seeded by seed_widgets.py, which "
            "scripts/verify-backup-restore.sh never runs. How to fix: run it with pipe_tool in "
            "the seed step, or name a tool it runs"
        )
    ]


def test_a_seeding_tool_missing_from_the_tools_directory_is_reported(tools_dir: Path) -> None:
    (tools_dir / _TOOL).unlink()

    assert _check(tools_dir) == [
        (
            "ps_state.public.widgets: declared seeded by seed_widgets.py, which is not in "
            "tools/backup-restore-verify. How to fix: name an existing seed tool"
        )
    ]


def test_bookkeeping_tables_need_no_seeder(tools_dir: Path) -> None:
    assert _check(tools_dir, created={_WIDGETS}) == []
    assert not BOOKKEEPING_TABLES & tables_created_by_migrations(_real_databases())


def test_a_bookkeeping_table_on_the_exception_list_is_reported(tools_dir: Path) -> None:
    assert _check(tools_dir, exceptions=["ps_state.public.ps_schema_migrations|runner table"]) == [
        (
            "ps_state.public.ps_schema_migrations: is a migration runner table, never empty "
            "once migrated; it cannot be a SEED_EMPTY_EXCEPTIONS entry. How to fix: remove the "
            "entry"
        )
    ]


def test_a_bookkeeping_table_declared_seeded_is_reported(tools_dir: Path) -> None:
    seeded = {_WIDGETS: _TOOL, "ps_signing.public.schema_migrations": _TOOL}

    assert _check(tools_dir, seeded=seeded) == [
        (
            "ps_signing.public.schema_migrations: is a migration runner table and needs no "
            "seeder. How to fix: remove it from guardrails/verifier_seeding.py"
        )
    ]


def test_the_verifier_tools_are_read_from_its_pipe_tool_calls() -> None:
    text = 'pipe_tool seed_content.py "$@"\n  pipe_tool graph_digests.py snapshot\npipe_tool x.py\n'

    assert _run_tools(text) == {"seed_content.py"}


def test_a_table_in_a_new_migration_directory_is_found(tmp_path: Path) -> None:
    directory = tmp_path / "widgets" / "storage_migrations"
    _write(
        directory / "0001_widgets.sql", "-- CREATE TABLE ignored (id int);\nCREATE TABLE w(i int);"
    )
    _write(directory / "notes.sql", "CREATE TABLE not_a_migration (id int);")

    directories = migration_directories(tmp_path)
    tables = tables_created_by_migrations(dict.fromkeys(directories, "ps_signing"))

    assert directories == [directory]
    assert tables == {"ps_signing.public.w"}


def test_a_schema_move_applies_whatever_the_directory_order(tmp_path: Path) -> None:
    privileged = tmp_path / "a_privileged" / "migrations"
    ordinary = tmp_path / "b_ordinary" / "migrations"
    _write(privileged / "0001_move.sql", "ALTER TABLE public.events SET SCHEMA audit_log;")
    _write(ordinary / "0001_events.sql", "CREATE TABLE IF NOT EXISTS events (id int);")

    tables = tables_created_by_migrations({privileged: "ps_state", ordinary: "ps_state"})

    assert tables == {"ps_state.audit_log.events"}


def test_a_migration_directory_with_no_registered_database_is_reported(tmp_path: Path) -> None:
    directory = tmp_path / "widgets" / "migrations"
    _write(directory / "0001_widgets.sql", "CREATE TABLE widgets (id int);")

    databases, problems = _databases(REGISTRY, [directory], REPO_ROOT)

    assert databases == {}
    assert problems == [
        (
            f"{directory}: has migrations but no database in guardrails/migration_registry.py. "
            "How to fix: register it (migration coverage guard)"
        )
    ]


# --- the real tree ---------------------------------------------------------------------------


def test_tables_created_by_the_migrations_include_the_moved_audit_table_and_both_databases() -> (
    None
):
    tables = tables_created_by_migrations(_real_databases())

    assert {
        "ps_state.audit_log.audit_events",
        "ps_state.audit_log.audit_chain_head",
        "ps_state.public.runtime_config",
        "ps_state.public.ingestion_runs",
        "ps_state.public.access_role_assignments",
        "ps_state.graph_log.entries",
        "ps_signing.public.pending_approvals",
        "ps_signing.public.signing_credentials",
    } <= tables
    assert "ps_state.public.audit_events" not in tables
    assert {
        "ps_state.public.ps_schema_migrations",
        "ps_signing.public.schema_migrations",
    } == BOOKKEEPING_TABLES


@pytest.mark.parametrize("name", ["VOLATILE_COUNT_TABLES", "SEED_EMPTY_EXCEPTIONS"])
def test_volatile_list_names_only_existing_tables(tmp_path: Path, name: str) -> None:
    created = tables_created_by_migrations(_real_databases())
    # A volatile count may be a runner table; a seed exception may not (see the checker).
    known = created | BOOKKEEPING_TABLES if name == "VOLATILE_COUNT_TABLES" else created

    assert _list_problems(_script_list(tmp_path, name), known) == []


def test_a_listed_table_that_no_migration_creates_is_reported() -> None:
    known = tables_created_by_migrations(_real_databases())

    assert _list_problems(["ps_state.public.no_such_table|why"], known) == [
        (
            "ps_state.public.no_such_table is not a table of ps_state or ps_signing. "
            "How to fix: remove or correct the entry in scripts/verify-backup-restore.sh"
        )
    ]
    assert _list_problems(["ps_state.graph_log.entries"], known) == [
        (
            "ps_state.graph_log.entries has no reason. "
            "How to fix: write the entry as `<table>|<reason>` in scripts/verify-backup-restore.sh"
        )
    ]
    assert _list_problems(["ps_state.graph_log.entries|rewritten by replay"], known) == []


def test_every_migration_table_is_seeded_or_excepted(tmp_path: Path) -> None:
    problems = _verifier_table_problems(
        created=tables_created_by_migrations(_real_databases()),
        seeded=SEEDED_TABLES,
        exceptions=_script_list(tmp_path, "SEED_EMPTY_EXCEPTIONS"),
        run_tools=_run_tools(SCRIPT.read_text()),
        tools_dir=TOOLS_DIR,
    )

    assert problems == [], "\n".join(problems)
