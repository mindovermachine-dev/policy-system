"""`postgres_live` tests: how the provisioning CLI fails (issue #205 follow-up, AC-FR-004).

A failing ordinary migration or an unobtainable migration lock must exit non-zero with one
sanitized line (migration name and exception class, or the fixed lock message -- never SQL, a
host or the admin password) and must not create any `graph_log` object. Real Postgres, real
runner, real migration files: the failure is provoked by pre-creating a conflicting table, not
by a fake.

Deselected by default -- run with `uv run pytest -m postgres_live`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from graph_gateway.test_provision_ordinary_live import ADMIN_PASSWORD, cli_environ, scalar
from ps_service.graph_gateway.provision import main
from ps_service.persistence.migration_runner import MIGRATION_LOCK_KEY

if TYPE_CHECKING:
    from persistence.provisioned_postgres import Provisioned

pytestmark = pytest.mark.postgres_live

_BANNER = "graph_gateway provisioning failed: "


def _assert_nothing_graph_log(prov: Provisioned) -> None:
    with prov.superuser_connect(prov.state_db) as conn:
        assert scalar(conn, "SELECT to_regnamespace('graph_log') IS NULL") is True
        assert (
            scalar(
                conn,
                "SELECT count(*) FROM ps_schema_migrations WHERE component = 'graph_gateway'",
            )
            == 0
        )


def test_failing_ordinary_migration_exits_nonzero_names_it_and_creates_no_graph_log(
    fresh_provisioned: Provisioned, capsys: pytest.CaptureFixture[str]
) -> None:
    with fresh_provisioned.as_state() as conn:
        conn.execute("CREATE TABLE public.audit_events (id int)")  # no tracking row

    exit_code = main([], cli_environ(fresh_provisioned))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == (
        f"{_BANNER}migration audit/0001_audit_events.sql failed to apply (DuplicateTable)\n"
    )
    assert ADMIN_PASSWORD not in captured.err
    assert fresh_provisioned.host not in captured.err
    _assert_nothing_graph_log(fresh_provisioned)


def test_failure_after_partial_ordinary_progress_keeps_that_progress_and_the_next_run_resumes(
    fresh_provisioned: Provisioned, capsys: pytest.CaptureFixture[str]
) -> None:
    with fresh_provisioned.as_state() as conn:
        conn.execute("CREATE TABLE public.runtime_config (id int)")

    assert main([], cli_environ(fresh_provisioned)) == 1
    err = capsys.readouterr().err
    with fresh_provisioned.as_state() as conn:
        done = conn.execute(
            "SELECT component FROM ps_schema_migrations ORDER BY component, filename"
        ).fetchall()
        conn.execute("DROP TABLE public.runtime_config")

    assert "migration runtime_config/0001_runtime_config.sql failed to apply" in err
    assert [row[0] for row in done] == ["audit", "audit", "authz"]
    assert main([], cli_environ(fresh_provisioned)) == 0
    capsys.readouterr()


def test_unobtainable_migration_lock_exits_nonzero_with_the_fixed_message(
    fresh_provisioned: Provisioned, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = {**cli_environ(fresh_provisioned), "PS_STATE_PROVISION_LOCK_TIMEOUT_SECONDS": "1"}
    with fresh_provisioned.as_state() as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))

        exit_code = main([], environ)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == (
        f"{_BANNER}migration tracking table setup could not acquire the migration lock within 1s\n"
    )
    assert ADMIN_PASSWORD not in captured.err
    with fresh_provisioned.superuser_connect(fresh_provisioned.state_db) as conn:
        assert scalar(conn, "SELECT to_regnamespace('graph_log') IS NULL") is True
