"""Tests for `ps_service.persistence.connection` (issue #130, PLAN.md Slice 1).

The connection helper and connectivity probe moved out of `ps_service.authz.store`;
these tests pin their fail-closed behaviour under the new `state_postgres_*` config
surface. No Postgres is needed: the unconfigured case never opens a connection and the
unreachable case targets a closed loopback port.
"""

from __future__ import annotations

import pytest

from ps_service.config import ServiceConfig
from ps_service.dependency_health import STATE_POSTGRES, is_healthy
from ps_service.persistence import (
    StatePostgresConnectionError,
    check_connectivity_from_config,
    connect_from_config,
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
