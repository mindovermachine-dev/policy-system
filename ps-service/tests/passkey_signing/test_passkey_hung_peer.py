"""A passkey-signing Postgres that accepts the TCP connection but never answers fails fast.

The peer is a real local socket that goes silent, so the connect timeout from `ServiceConfig`
reaches libpq for real. libpq treats a connect timeout below 2 s as 2 s, hence the 2 s setting.
"""

from __future__ import annotations

import time

import psycopg
import pytest
from graph_gateway.silent_peer import silent_peer

from ps_service.config import ServiceConfig
from ps_service.passkey_signing import connect_for_migrations, connect_from_config
from ps_service.passkey_signing.errors import PasskeySigningPostgresConnectionError
from ps_service.passkey_signing.store import check_connectivity_from_config

_CONNECT_TIMEOUT_SECONDS = 2
_TOLERANCE_SECONDS = 2.0


def _config(port: int) -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        passkey_signing_postgres_host="127.0.0.1",
        passkey_signing_postgres_port=port,
        passkey_signing_postgres_database="ps_signing",
        passkey_signing_postgres_user="ps_signing",
        passkey_signing_postgres_password="unused",
        passkey_signing_postgres_connect_timeout_seconds=_CONNECT_TIMEOUT_SECONDS,
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


def test_the_connectivity_probe_of_a_silent_peer_fails_within_the_bound() -> None:
    with silent_peer() as peer:
        started = time.monotonic()
        with pytest.raises(PasskeySigningPostgresConnectionError):
            check_connectivity_from_config(_config(peer.port))
        elapsed = time.monotonic() - started

    assert elapsed <= _CONNECT_TIMEOUT_SECONDS + _TOLERANCE_SECONDS
