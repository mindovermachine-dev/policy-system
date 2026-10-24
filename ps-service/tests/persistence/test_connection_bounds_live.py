"""The configured bounds reach the server for request-path and migration connections."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from ps_service.persistence import (
    apply_pending_migrations,
    connect_for_migrations,
    connect_from_config,
)
from ps_service.state_migrations import ORDINARY_STATE_MIGRATION_SOURCES

if TYPE_CHECKING:
    from persistence.provisioned_postgres import Provisioned
    from ps_service.config import ServiceConfig

pytestmark = pytest.mark.postgres_live


def _bounded(prov: Provisioned, *, statement_timeout_ms: int) -> ServiceConfig:
    return dataclasses.replace(
        prov.state_config(),
        state_postgres_connect_timeout_seconds=7,
        state_postgres_statement_timeout_ms=statement_timeout_ms,
    )


def test_a_request_path_connection_applies_the_configured_statement_timeout(
    provisioned: Provisioned,
) -> None:
    config = _bounded(provisioned, statement_timeout_ms=100)

    with connect_from_config(config) as conn:
        shown = conn.execute("SHOW statement_timeout").fetchone()
        with pytest.raises(psycopg.errors.QueryCanceled):
            conn.execute("SELECT pg_sleep(1)")

    assert shown == ("100ms",)


def test_a_request_path_connection_carries_the_configured_connect_timeout(
    provisioned: Provisioned,
) -> None:
    config = _bounded(provisioned, statement_timeout_ms=100)

    with connect_from_config(config) as conn:
        parameters = conninfo_to_dict(conn.info.dsn)

    assert parameters["connect_timeout"] == "7"


def test_a_migration_connection_has_no_statement_timeout(
    provisioned: Provisioned,
) -> None:
    config = _bounded(provisioned, statement_timeout_ms=100)

    with connect_for_migrations(config) as conn:
        shown = conn.execute("SHOW statement_timeout").fetchone()
        conn.execute("SELECT pg_sleep(0.3)")
        parameters = conninfo_to_dict(conn.info.dsn)

    assert shown == ("0",)
    assert parameters["connect_timeout"] == "7"


def test_migrations_apply_on_a_migration_connection_despite_a_one_millisecond_statement_timeout(
    fresh_provisioned: Provisioned,
) -> None:
    config = _bounded(fresh_provisioned, statement_timeout_ms=1)

    with connect_for_migrations(config) as conn:
        apply_pending_migrations(conn, sources=ORDINARY_STATE_MIGRATION_SOURCES)
        recorded = conn.execute("SELECT count(*) FROM ps_schema_migrations").fetchone()

    assert recorded is not None
    assert recorded[0] > 0
