"""Live proof that upgrading through any migration prefix gives the schema of a fresh apply.

The cases come from the migration directories themselves: for each multi-file component (the
ordinary state components and passkey signing) there is one case per proper prefix of its sorted
`*.sql` files, so a newly added migration file adds a case with no edit here. Each case applies
the prefix, then the full directory, to one schema and the full directory once to another, and
requires the two structures to match and the second step to apply exactly the remaining files.
Single-file components (authz, runtime_config, ingestion_runs) produce no prefix cases; their
schema is covered by their own shape tests.

The live test is deselected by default; select with
`uv run pytest -m postgres_live
ps-service/tests/persistence/test_migration_prefix_equivalence_live.py`.
The guard test runs without a database.

Start a scratch Postgres:
`podman run -d --name <name> -p 54329:5432 -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16-alpine`
and export `PS_STATE_POSTGRES_HOST`, `PS_STATE_POSTGRES_PORT`, `PS_STATE_POSTGRES_DATABASE`
and `PS_STATE_POSTGRES_USER` (e.g. `localhost`, `54329`, `postgres`, `postgres`). For the whole
`-m postgres_live` suite also export `PS_TEST_POSTGRES_SUPERUSER_DSN` (e.g.
`postgresql://postgres@localhost:54329/postgres`) with `psql` on `PATH`, because neighbouring
modules read it. `-m postgres_live` runs in a single process.

Each case works in two uuid-named schemas, dropped on teardown.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING, NamedTuple

import pytest

from persistence.live_schema_support import (
    apply_passkey_directory,
    copy_migration_prefix,
    isolated_schema,
    passkey_migrations_dir,
    require_configured_state_postgres,
    schema_snapshot,
)
from ps_service.persistence import MigrationSource, apply_pending_migrations
from ps_service.state_migrations import ORDINARY_STATE_MIGRATION_SOURCES

if TYPE_CHECKING:
    from pathlib import Path

    from psycopg import Connection
    from psycopg.rows import TupleRow

PASSKEY_COMPONENT = "passkey_signing"


class PrefixCase(NamedTuple):
    """One component and the number of leading migration files applied first."""

    component: str
    directory: Path
    prefix_length: int
    filenames: tuple[str, ...]


def _component_directories() -> dict[str, Path]:
    directories = {
        source.component: source.directory for source in ORDINARY_STATE_MIGRATION_SOURCES
    }
    directories[PASSKEY_COMPONENT] = passkey_migrations_dir()
    return directories


def _prefix_cases() -> list[PrefixCase]:
    cases: list[PrefixCase] = []
    for component, directory in _component_directories().items():
        filenames = tuple(path.name for path in sorted(directory.glob("*.sql")))
        cases.extend(
            PrefixCase(component, directory, length, filenames)
            for length in range(1, len(filenames))
        )
    return cases


def _case_id(case: PrefixCase) -> str:
    return f"{case.component}-first-{case.prefix_length}-files"


PREFIX_CASES = _prefix_cases()


def _apply(
    conn: Connection[TupleRow],
    component: str,
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    if component == PASSKEY_COMPONENT:
        return apply_passkey_directory(conn, directory, monkeypatch)
    return apply_pending_migrations(conn, sources=[MigrationSource(component, directory)])


def test_prefix_cases_cover_audit_and_passkey_up_to_the_second_to_last_file() -> None:
    longest_prefix: dict[str, int] = {}
    for case in PREFIX_CASES:
        longest_prefix[case.component] = max(
            longest_prefix.get(case.component, 0), case.prefix_length
        )

    assert {"audit", PASSKEY_COMPONENT} <= set(longest_prefix)
    assert all(longest_prefix[case.component] == len(case.filenames) - 1 for case in PREFIX_CASES)


@pytest.mark.postgres_live
@pytest.mark.parametrize("case", PREFIX_CASES, ids=_case_id)
def test_upgrading_through_a_prefix_gives_the_schema_of_a_fresh_apply(
    case: PrefixCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_configured_state_postgres()
    prefix_dir = copy_migration_prefix(case.directory, case.prefix_length, tmp_path / "prefix")
    with ExitStack() as stack:
        upgraded, upgraded_schema = stack.enter_context(isolated_schema())
        fresh, fresh_schema = stack.enter_context(isolated_schema())

        _apply(upgraded, case.component, prefix_dir, monkeypatch)
        applied_on_upgrade = _apply(upgraded, case.component, case.directory, monkeypatch)
        _apply(fresh, case.component, case.directory, monkeypatch)

        assert applied_on_upgrade == list(case.filenames[case.prefix_length :])
        assert schema_snapshot(upgraded, upgraded_schema) == schema_snapshot(fresh, fresh_schema)
