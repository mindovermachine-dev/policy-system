"""PS state Postgres connection helper and connectivity probe (issue #130).

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

    Mirrors `ps_service.passkey_signing.store.connect_from_config`'s
    per-call-connection idiom, with one deliberate divergence (PLAN.md
    §0.11): raises `StatePostgresConnectionError` immediately when
    `config.state_postgres_host` is `None`, without attempting a doomed
    `psycopg.connect(host=None, ...)` call -- every state-store caller must
    fail closed rather than silently no-op.
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
    """Probe the PS state Postgres instance.

    Unlike `ps_service.passkey_signing.store.check_connectivity_from_config`
    (a no-op when unconfigured), an unconfigured PS state Postgres is treated
    as unreachable too (PLAN.md §0.11/§2.2): every role-gated action must
    fail closed when this store cannot be reached, so "unconfigured" is not
    a healthy "not applicable" state here -- it is the store being down.

    Raises:
        StatePostgresConnectionError: unconfigured, or configured but
            unreachable (connection failure or the round-trip query itself
            fails); the outcome is also recorded in
            `ps_service.dependency_health`.
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
