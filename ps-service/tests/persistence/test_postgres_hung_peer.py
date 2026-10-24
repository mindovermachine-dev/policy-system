"""A state Postgres that accepts the TCP connection but never answers fails within the bound.

The peer is a real local socket that goes silent, so the connect timeout from `ServiceConfig`
reaches libpq for real. libpq treats a connect timeout below 2 s as 2 s, hence the 2 s setting.
"""

from __future__ import annotations

import time

import psycopg
import pytest
from graph_gateway.silent_peer import silent_peer

from ps_service.config import ServiceConfig
from ps_service.persistence import connect_for_migrations, connect_from_config

_CONNECT_TIMEOUT_SECONDS = 2
_TOLERANCE_SECONDS = 2.0


def _config(port: int) -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        state_postgres_host="127.0.0.1",
        state_postgres_port=port,
        state_postgres_database="ps_state",
        state_postgres_user="ps_state",
        state_postgres_password="unused",
        state_postgres_connect_timeout_seconds=_CONNECT_TIMEOUT_SECONDS,
    )


def test_a_request_path_connect_to_a_silent_peer_times_out_within_the_bound() -> None:
    with silent_peer() as peer:
        started = time.monotonic()
        with pytest.raises(psycopg.OperationalError):
            connect_from_config(_config(peer.port))
        elapsed = time.monotonic() - started

    assert elapsed <= _CONNECT_TIMEOUT_SECONDS + _TOLERANCE_SECONDS


def test_a_migration_connect_to_a_silent_peer_times_out_within_the_bound() -> None:
    with silent_peer() as peer:
        started = time.monotonic()
        with pytest.raises(psycopg.OperationalError):
            connect_for_migrations(_config(peer.port))
        elapsed = time.monotonic() - started

    assert elapsed <= _CONNECT_TIMEOUT_SECONDS + _TOLERANCE_SECONDS
