"""`postgres_live`: the passkey-signing Postgres bounds and sanitized errors, on a real server.

The cluster is built by the real Helm init script. Request-path connections carry the configured
connect and statement timeouts; migration connections carry only the connect timeout. A second
connection holds an exclusive lock on a table, so a store's statement is cancelled by the real
server-side timeout; a store must then raise its existing error type with a message free of SQL,
host and driver text, and keep the driver error as the cause. A closed local port is sanitized
the same way.
"""

from __future__ import annotations

import contextlib
import dataclasses
import socket
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from ps_service.config import ServiceConfig, load_config
from ps_service.passkey_signing import connect_for_migrations, connect_from_config
from ps_service.passkey_signing.errors import (
    PendingApprovalPersistenceError,
    SigningCredentialPersistenceError,
)
from ps_service.passkey_signing.migration_runner import apply_pending_migrations
from ps_service.passkey_signing.signing_credential_store import PsycopgSigningCredentialStore
from ps_service.passkey_signing.store import PsycopgPendingApprovalStore

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from persistence.provisioned_postgres import Provisioned

pytestmark = pytest.mark.postgres_live

_STATEMENT_TIMEOUT_MS = 100
_ACTOR = ("subject-1", "https://issuer.example.com/")
_FORBIDDEN = ("127.0.0.1", "psycopg", "statement timeout", "canceling", "SELECT", "INSERT")

type _Call = Callable[[ServiceConfig], object]


def _config(prov: Provisioned, *, host: str, port: int) -> ServiceConfig:
    return dataclasses.replace(
        load_config(),
        passkey_signing_postgres_host=host,
        passkey_signing_postgres_port=port,
        passkey_signing_postgres_database=prov.signing_db,
        passkey_signing_postgres_user=prov.signing_user,
        passkey_signing_postgres_password=prov.signing_password,
        passkey_signing_postgres_connect_timeout_seconds=7,
        passkey_signing_postgres_statement_timeout_ms=_STATEMENT_TIMEOUT_MS,
    )


@pytest.fixture(scope="module")
def migrated(provisioned: Provisioned) -> ServiceConfig:
    config = _config(provisioned, host=provisioned.host, port=provisioned.port)
    with connect_for_migrations(config) as conn:
        apply_pending_migrations(conn)
    return config


def _create_approval(config: ServiceConfig) -> object:
    return PsycopgPendingApprovalStore(config).create_pending_approval(
        tool_name="merge-capabilities",
        normalized_args={},
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        display_summary={},
    )


def _get_approval(config: ServiceConfig) -> object:
    return PsycopgPendingApprovalStore(config).get_by_id("approval-1")


def _create_credential(config: ServiceConfig) -> object:
    return PsycopgSigningCredentialStore(config).create_signing_credential(
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        credential_id=b"credential",
        public_key=b"key",
        sign_count=0,
        rp_id="localhost",
    )


def _list_credentials(config: ServiceConfig) -> object:
    return PsycopgSigningCredentialStore(config).list_for_actor(
        actor_subject=_ACTOR[0], actor_issuer=_ACTOR[1], rp_id="localhost"
    )


def _get_credential(config: ServiceConfig) -> object:
    return PsycopgSigningCredentialStore(config).get_by_credential_id(b"credential")


def _update_sign_count(config: ServiceConfig) -> object:
    return PsycopgSigningCredentialStore(config).update_sign_count(
        credential_id=b"credential", sign_count=1
    )


@dataclasses.dataclass(frozen=True)
class _Operation:
    name: str
    call: _Call
    error: type[Exception]
    locked_table: str


_OPERATIONS = (
    _Operation(
        "create_approval", _create_approval, PendingApprovalPersistenceError, "pending_approvals"
    ),
    _Operation("get_approval", _get_approval, PendingApprovalPersistenceError, "pending_approvals"),
    _Operation(
        "create_credential",
        _create_credential,
        SigningCredentialPersistenceError,
        "signing_credentials",
    ),
    _Operation(
        "list_credentials",
        _list_credentials,
        SigningCredentialPersistenceError,
        "signing_credentials",
    ),
    _Operation(
        "get_credential", _get_credential, SigningCredentialPersistenceError, "signing_credentials"
    ),
    _Operation(
        "update_sign_count",
        _update_sign_count,
        SigningCredentialPersistenceError,
        "signing_credentials",
    ),
)
_IDS = [operation.name for operation in _OPERATIONS]


@contextlib.contextmanager
def _table_locked(prov: Provisioned, table: str) -> Generator[None]:
    """Hold an exclusive lock on `table` from a second connection for the block."""
    with prov.superuser_connect(prov.signing_db) as holder, holder.transaction():
        holder.execute(
            sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(sql.Identifier(table))
        )
        yield


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _assert_sanitized(raised: pytest.ExceptionInfo[Exception]) -> None:
    message = str(raised.value)
    assert message.startswith("failed to ")
    assert not [leak for leak in _FORBIDDEN if leak in message]
    assert isinstance(raised.value.__cause__, psycopg.Error)


def test_a_request_path_connection_applies_the_configured_statement_timeout(
    migrated: ServiceConfig,
) -> None:
    with connect_from_config(migrated) as conn:
        shown = conn.execute("SHOW statement_timeout").fetchone()
        parameters = conninfo_to_dict(conn.info.dsn)
        with pytest.raises(psycopg.errors.QueryCanceled):
            conn.execute("SELECT pg_sleep(1)")

    assert shown == ("100ms",)
    assert parameters["connect_timeout"] == "7"


def test_a_migration_connection_has_no_statement_timeout(migrated: ServiceConfig) -> None:
    with connect_for_migrations(migrated) as conn:
        shown = conn.execute("SHOW statement_timeout").fetchone()
        conn.execute("SELECT pg_sleep(0.3)")
        parameters = conninfo_to_dict(conn.info.dsn)

    assert shown == ("0",)
    assert parameters["connect_timeout"] == "7"


@pytest.mark.parametrize("operation", _OPERATIONS, ids=_IDS)
def test_a_statement_cancelled_by_the_server_surfaces_the_stores_sanitized_error(
    provisioned: Provisioned, migrated: ServiceConfig, operation: _Operation
) -> None:
    with (
        _table_locked(provisioned, operation.locked_table),
        pytest.raises(operation.error) as raised,
    ):
        operation.call(migrated)

    _assert_sanitized(raised)
    assert isinstance(raised.value.__cause__, psycopg.errors.QueryCanceled)


@pytest.mark.parametrize("operation", _OPERATIONS, ids=_IDS)
def test_a_refused_connection_surfaces_the_stores_sanitized_error(
    provisioned: Provisioned, operation: _Operation
) -> None:
    port = _closed_port()
    config = _config(provisioned, host="127.0.0.1", port=port)

    with pytest.raises(operation.error) as raised:
        operation.call(config)

    _assert_sanitized(raised)
    assert str(port) not in str(raised.value)
