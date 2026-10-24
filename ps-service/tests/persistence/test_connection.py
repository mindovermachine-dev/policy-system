"""Tests for `ps_service.persistence.connection`.

The connection helper and connectivity probe moved out of `ps_service.authz.store`;
these tests pin their fail-closed behaviour under the new `state_postgres_*` config
surface. No Postgres is needed: the unconfigured case never opens a connection and the
unreachable case targets a closed loopback port.
"""

from __future__ import annotations

import dataclasses

import pytest
from psycopg.conninfo import conninfo_to_dict

from ps_service.config import ServiceConfig
from ps_service.dependency_health import STATE_POSTGRES, is_healthy
from ps_service.persistence import (
    StatePostgresConnectionError,
    check_connectivity_from_config,
    connect_from_config,
)
from ps_service.persistence.connection import (
    _connection_conninfo,  # pyright: ignore[reportPrivateUsage]  # pure argument builder, tested directly
    _migration_conninfo,  # pyright: ignore[reportPrivateUsage]  # pure argument builder, tested directly
)


def _config(*, host: str | None, port: int = 5432) -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        state_postgres_host=host,
        state_postgres_port=port,
        state_postgres_database="ps_state",
        state_postgres_user="ps_state",
        state_postgres_password="unused",
    )


def test_connect_from_config_raises_state_postgres_connection_error_when_host_unset() -> None:
    """An unconfigured store fails closed immediately, naming `PS_STATE_POSTGRES_HOST`."""
    with pytest.raises(StatePostgresConnectionError, match="PS_STATE_POSTGRES_HOST"):
        connect_from_config(_config(host=None))


def test_check_connectivity_marks_state_postgres_unhealthy_and_raises_when_unreachable() -> None:
    """A configured-but-unreachable store raises and is recorded unhealthy in dependency health."""
    with pytest.raises(StatePostgresConnectionError):
        check_connectivity_from_config(_config(host="127.0.0.1", port=59999))

    assert is_healthy(STATE_POSTGRES) is False


def test_check_connectivity_marks_state_postgres_unhealthy_when_host_unset() -> None:
    """Unconfigured is treated as unreachable (never a healthy "not applicable")."""
    with pytest.raises(StatePostgresConnectionError):
        check_connectivity_from_config(_config(host=None))

    assert is_healthy(STATE_POSTGRES) is False


def test_request_path_conninfo_applies_the_default_bounds_from_config() -> None:
    arguments = conninfo_to_dict(
        _connection_conninfo(_config(host="db"), connect_timeout_s=None, statement_timeout_ms=None)
    )

    assert arguments["connect_timeout"] == "5"
    assert arguments.get("options") == "-c statement_timeout=30000"
    assert arguments["host"] == "db"


def test_request_path_conninfo_honors_configured_bounds_and_round_connect_up() -> None:
    config = dataclasses.replace(
        _config(host="db"),
        state_postgres_connect_timeout_seconds=7.2,
        state_postgres_statement_timeout_ms=1234,
    )

    arguments = conninfo_to_dict(
        _connection_conninfo(config, connect_timeout_s=None, statement_timeout_ms=None)
    )

    assert arguments["connect_timeout"] == "8"
    assert arguments.get("options") == "-c statement_timeout=1234"


def test_request_path_conninfo_prefers_explicit_bounds_over_config() -> None:
    arguments = conninfo_to_dict(
        _connection_conninfo(_config(host="db"), connect_timeout_s=2, statement_timeout_ms=2000)
    )

    assert arguments["connect_timeout"] == "2"
    assert arguments.get("options") == "-c statement_timeout=2000"


def test_migration_conninfo_bounds_the_connect_but_not_statements() -> None:
    config = dataclasses.replace(
        _config(host="db"),
        state_postgres_connect_timeout_seconds=3,
        state_postgres_statement_timeout_ms=100,
    )

    arguments = conninfo_to_dict(_migration_conninfo(config))

    assert arguments["connect_timeout"] == "3"
    assert "options" not in arguments
