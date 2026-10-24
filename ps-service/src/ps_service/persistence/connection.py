"""PS state Postgres connection helper and connectivity probe.

Shared by every component that persists to the PS state Postgres
(`ps_service.audit`, `ps_service.authz`); none of them owns the connection
config, `ps_service.persistence` does.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import psycopg
from psycopg.conninfo import make_conninfo

from ps_service.dependency_health import STATE_POSTGRES, mark_healthy, mark_unhealthy
from ps_service.persistence.errors import StatePostgresConnectionError

if TYPE_CHECKING:
    from psycopg.rows import TupleRow

    from ps_service.config import ServiceConfig


def _unconfigured_error() -> StatePostgresConnectionError:
    return StatePostgresConnectionError(
        "PS state Postgres is not configured (PS_STATE_POSTGRES_HOST is unset); "
        "every role-gated action fails closed until it is configured."
    )


def _conninfo(config: ServiceConfig, connect_timeout_s: float, options: str | None) -> str:
    return make_conninfo(
        host=config.state_postgres_host,
        port=config.state_postgres_port,
        dbname=config.state_postgres_database,
        user=config.state_postgres_user,
        password=config.state_postgres_password,
        connect_timeout=max(1, math.ceil(connect_timeout_s)),
        options=options,
    )


def _connection_conninfo(
    config: ServiceConfig,
    *,
    connect_timeout_s: float | None,
    statement_timeout_ms: int | None,
) -> str:
    """Return the libpq connection string for a request-path connection.

    An explicit bound wins; `None` falls back to the configured value, so the result always
    carries both a connect timeout and a statement timeout.
    """
    connect_timeout = (
        config.state_postgres_connect_timeout_seconds
        if connect_timeout_s is None
        else connect_timeout_s
    )
    statement_timeout = (
        config.state_postgres_statement_timeout_ms
        if statement_timeout_ms is None
        else statement_timeout_ms
    )
    return _conninfo(config, connect_timeout, f"-c statement_timeout={statement_timeout}")


def _migration_conninfo(config: ServiceConfig) -> str:
    """Return the libpq connection string for a migration connection: connect bound only."""
    return _conninfo(config, config.state_postgres_connect_timeout_seconds, None)


def connect_from_config(
    config: ServiceConfig,
    *,
    connect_timeout_s: float | None = None,
    statement_timeout_ms: int | None = None,
) -> psycopg.Connection[TupleRow]:
    """Open a fresh bounded `psycopg` connection from `config.state_postgres_*`.

    Each call opens its own connection; the caller owns it and must close it. When
    `config.state_postgres_host` is `None` it raises immediately instead of attempting a
    doomed `psycopg.connect(host=None, ...)` call, because every state-store caller must
    fail closed rather than silently no-op.

    Args:
        config: Process configuration; supplies host, port, database, user and password of
            the PS state Postgres, and the default connect and statement timeouts.
            `state_postgres_host` must not be `None`.
        connect_timeout_s: Longest the connection attempt may take, rounded up to whole seconds
            (libpq's resolution); `None` uses `config.state_postgres_connect_timeout_seconds`.
        statement_timeout_ms: Server-side limit applied to every statement on the connection,
            passed as a connection option so it costs no round trip; `None` uses
            `config.state_postgres_statement_timeout_ms`.

    Returns:
        An open connection with the default tuple row factory.

    Raises:
        StatePostgresConnectionError: `config.state_postgres_host` is `None`.
        psycopg.OperationalError: the instance is configured but the connection fails or does
            not complete within the connect timeout (propagated from `psycopg.connect`).

    Side Effects:
        Opens one network connection to the PS state Postgres.
    """
    if config.state_postgres_host is None:
        raise _unconfigured_error()
    return psycopg.connect(
        _connection_conninfo(
            config, connect_timeout_s=connect_timeout_s, statement_timeout_ms=statement_timeout_ms
        )
    )


def connect_for_migrations(config: ServiceConfig) -> psycopg.Connection[TupleRow]:
    """Open a bounded connection for startup migrations: connect timeout, no statement timeout.

    Migrations and the privileged-migration verification legitimately run statements longer than
    the request-path statement timeout, so only establishing the connection is bounded.

    Args:
        config: Process configuration; supplies the PS state Postgres coordinates and
            `state_postgres_connect_timeout_seconds`. `state_postgres_host` must not be `None`.

    Returns:
        An open connection with the default tuple row factory; the caller closes it.

    Raises:
        StatePostgresConnectionError: `config.state_postgres_host` is `None`.
        psycopg.OperationalError: the connection fails or does not complete within the connect
            timeout.

    Side Effects:
        Opens one network connection to the PS state Postgres.
    """
    if config.state_postgres_host is None:
        raise _unconfigured_error()
    return psycopg.connect(_migration_conninfo(config))


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
            failure, the connect or statement timeout elapsing, or the round-trip query itself
            failing). A driver failure is chained via
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
