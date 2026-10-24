"""The passkey-signing Postgres connections carry the configured bounds."""

from __future__ import annotations

from psycopg.conninfo import conninfo_to_dict

from ps_service.config import ServiceConfig
from ps_service.passkey_signing.store import (
    _connection_conninfo,  # pyright: ignore[reportPrivateUsage]  # pure argument builder, tested directly
    _migration_conninfo,  # pyright: ignore[reportPrivateUsage]  # pure argument builder, tested directly
)


def _config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        passkey_signing_postgres_host="db",
        passkey_signing_postgres_port=5432,
        passkey_signing_postgres_database="ps_signing",
        passkey_signing_postgres_user="ps_signing",
        passkey_signing_postgres_password="unused",
        passkey_signing_postgres_connect_timeout_seconds=3.2,
        passkey_signing_postgres_statement_timeout_ms=100,
    )


def test_request_path_conninfo_applies_the_configured_bounds() -> None:
    arguments = conninfo_to_dict(
        _connection_conninfo(_config(), connect_timeout_s=None, statement_timeout_ms=None)
    )

    assert arguments["host"] == "db"
    assert arguments["connect_timeout"] == "4"
    assert arguments["options"] == "-c statement_timeout=100"


def test_request_path_conninfo_prefers_explicit_bounds_over_config() -> None:
    arguments = conninfo_to_dict(
        _connection_conninfo(_config(), connect_timeout_s=2, statement_timeout_ms=2000)
    )

    assert arguments["connect_timeout"] == "2"
    assert arguments["options"] == "-c statement_timeout=2000"


def test_migration_conninfo_bound_the_connect_but_not_statements() -> None:
    arguments = conninfo_to_dict(_migration_conninfo(_config()))

    assert arguments["host"] == "db"
    assert arguments["connect_timeout"] == "4"
    assert "options" not in arguments
