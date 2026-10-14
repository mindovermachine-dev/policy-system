"""PS state Postgres connection helper and connectivity probe.

Shared by every component that persists to the PS state Postgres
(`ps_service.audit`, `ps_service.authz`); none of them owns the connection
config, `ps_service.persistence` does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg

from ps_service.dependency_health import STATE_POSTGRES, mark_healthy, mark_unhealthy
from ps_service.persistence.errors import StatePostgresConnectionError

if TYPE_CHECKING:
    from psycopg.rows import TupleRow

    from ps_service.config import ServiceConfig


def connect_from_config(config: ServiceConfig) -> psycopg.Connection[TupleRow]:
    """Open a fresh `psycopg` connection from `config.state_postgres_*`.

    Each call opens its own connection; the caller owns it and must close it. When
    `config.state_postgres_host` is `None` it raises immediately instead of attempting a
    doomed `psycopg.connect(host=None, ...)` call, because every state-store caller must
    fail closed rather than silently no-op.

    Args:
        config: Process configuration; supplies host, port, database, user and password of
            the PS state Postgres. `state_postgres_host` must not be `None`.

    Returns:
        An open connection with the default tuple row factory.

    Raises:
        StatePostgresConnectionError: `config.state_postgres_host` is `None`.
        psycopg.OperationalError: the instance is configured but the connection fails
            (propagated from `psycopg.connect`).

    Side Effects:
        Opens one network connection to the PS state Postgres.
    """
    if config.state_postgres_host is None:
        raise StatePostgresConnectionError(
            "PS state Postgres is not configured (PS_STATE_POSTGRES_HOST is unset); "
            "every role-gated action fails closed until it is configured."
        )
    return psycopg.connect(
        host=config.state_postgres_host,
        port=config.state_postgres_port,
        dbname=config.state_postgres_database,
        user=config.state_postgres_user,
        password=config.state_postgres_password,
    )


def check_connectivity_from_config(config: ServiceConfig) -> None:
    """Probe the PS state Postgres instance with a `SELECT 1` round trip.

    An unconfigured PS state Postgres is treated as unreachable too: every role-gated action
    must fail closed when this store cannot be reached, so "unconfigured" is not a healthy
    "not applicable" state here -- it is the store being down.

    Args:
        config: Process configuration supplying the PS state Postgres coordinates.

    Returns:
        None; returning normally means the round trip succeeded.

    Raises:
        StatePostgresConnectionError: unconfigured, or configured but unreachable (connection
            failure or the round-trip query itself fails). A driver failure is chained via
            `from`.

    Side Effects:
        Opens and closes one connection, and records the outcome in
        `ps_service.dependency_health` (healthy on success, unhealthy on failure).
    """
    try:
        with connect_from_config(config) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except StatePostgresConnectionError as exc:
        mark_unhealthy(STATE_POSTGRES, error=exc)
        raise
    except psycopg.Error as exc:
        mark_unhealthy(STATE_POSTGRES, error=exc)
        raise StatePostgresConnectionError(
            "PS state Postgres connection failed at "
            f"{config.state_postgres_host}:{config.state_postgres_port}. "
            f"Is Postgres running? Error: {exc}"
        ) from exc
    mark_healthy(STATE_POSTGRES)
