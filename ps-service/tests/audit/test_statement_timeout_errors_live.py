"""`postgres_live`: a statement that exceeds its timeout, or a refused connection, is sanitized.

Every store that talks to the PS state Postgres is run against a real scratch cluster. A second
connection holds an exclusive lock on the table the store needs, so the store's statement is
cancelled by the real server-side statement timeout (100 ms). The store must raise its existing
error type with a message free of SQL, host and driver text, and keep the driver error as the
cause. A connection to a closed local port must be sanitized the same way. A cancelled statement
marks the state Postgres unhealthy, and the next successful call heals it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import socket
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg import sql

import ps_service.authz.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers access_role.* actions
from ps_service.audit.errors import (
    AuditPersistenceError,
    AuditPostgresUnavailableError,
    AuditTrailUnavailableError,
)
from ps_service.audit.models import AuditQueryFilters
from ps_service.audit.store import PsycopgAuditStore
from ps_service.authz.errors import AccessRoleAssignmentPersistenceError
from ps_service.authz.store import PsycopgAccessRoleStore
from ps_service.curated_source.config_key import CATALOG_SOURCE_KEY
from ps_service.dependency_health import STATE_POSTGRES, is_healthy
from ps_service.graph_gateway.errors import GraphLogUnavailableError
from ps_service.graph_gateway.store import PsycopgGraphLogStore
from ps_service.graph_gateway.system_audit import record_system_audit
from ps_service.ingestion_runs.errors import IngestionRunStoreUnavailableError
from ps_service.ingestion_runs.store import PsycopgIngestionRunStore
from ps_service.persistence import connect_from_config
from ps_service.runtime_config.errors import RuntimeConfigUnavailableError
from ps_service.runtime_config.store import PsycopgRuntimeConfigStore

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from persistence.provisioned_postgres import Provisioned

    from ps_service.config import ServiceConfig

pytestmark = pytest.mark.postgres_live

_STATEMENT_TIMEOUT_MS = 100
_ACTOR = ("subject-1", "https://issuer.example.com/")
_RUN_ID = "0b1c1d3e-6c0e-4a43-9a52-2f6a3e6a1d55"

type _Call = Callable[[ServiceConfig], object]


def _record_standalone(config: ServiceConfig) -> object:
    return PsycopgAuditStore(config).record_standalone(
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        action="access_role.grant",
        resource_type="principal",
        resource_id="target-1",
        outcome="applied",
        details={"access_role": "SystemAdmin"},
    )


def _query_audit(config: ServiceConfig) -> object:
    return PsycopgAuditStore(config).query(filters=AuditQueryFilters(), cursor=None, page_size=5)


def _verify_audit(config: ServiceConfig) -> object:
    return PsycopgAuditStore(config).verify_integrity()


def _count_owners(config: ServiceConfig) -> object:
    return PsycopgAccessRoleStore(
        config, audit_store=PsycopgAuditStore(config)
    ).count_active_system_owners()


def _get_runtime_config(config: ServiceConfig) -> object:
    return PsycopgRuntimeConfigStore(config, audit_store=PsycopgAuditStore(config)).get(
        CATALOG_SOURCE_KEY
    )


def _get_run(config: ServiceConfig) -> object:
    return PsycopgIngestionRunStore(config, audit_store=PsycopgAuditStore(config)).get_run(_RUN_ID)


def _read_applied_position(config: ServiceConfig) -> object:
    return PsycopgGraphLogStore(config).read_applied_position("compliance")


def _record_system_audit(config: ServiceConfig) -> object:
    return record_system_audit(
        lambda: connect_from_config(config),
        action="graph.system",
        resource_type="graph",
        resource_id="compliance",
        details={},
    )


@dataclasses.dataclass(frozen=True)
class _Family:
    name: str
    call: _Call
    error: type[Exception]
    locked_table: str
    refused_error: type[Exception] | None = None
    """Error for a refused connection when it differs from the cancelled-statement `error`."""


_FAMILIES = (
    _Family(
        "audit_record",
        _record_standalone,
        AuditPersistenceError,
        "audit_log.audit_events",
        refused_error=AuditPostgresUnavailableError,
    ),
    _Family("audit_query", _query_audit, AuditPostgresUnavailableError, "audit_log.audit_events"),
    _Family("audit_verify", _verify_audit, AuditPostgresUnavailableError, "audit_log.audit_events"),
    _Family(
        "authz", _count_owners, AccessRoleAssignmentPersistenceError, "access_role_assignments"
    ),
    _Family("runtime_config", _get_runtime_config, RuntimeConfigUnavailableError, "runtime_config"),
    _Family("ingestion_runs", _get_run, IngestionRunStoreUnavailableError, "ingestion_runs"),
    _Family(
        "graph_log",
        _read_applied_position,
        GraphLogUnavailableError,
        "graph_log.applied_markers",
    ),
    _Family(
        "system_audit",
        _record_system_audit,
        AuditTrailUnavailableError,
        "audit_log.audit_events",
    ),
)
_IDS = [family.name for family in _FAMILIES]
_FORBIDDEN = ("127.0.0.1", "psycopg", "statement timeout", "canceling", "SELECT", "INSERT", "lock")


@contextlib.contextmanager
def _table_locked(prov: Provisioned, table: str) -> Generator[None]:
    """Hold an exclusive lock on `table` from a second connection for the block."""
    with prov.superuser_connect(prov.state_db) as holder, holder.transaction():
        holder.execute(
            sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
                sql.Identifier(*table.split("."))
            )
        )
        yield


def _bounded(prov: Provisioned) -> ServiceConfig:
    return dataclasses.replace(
        prov.state_config(), state_postgres_statement_timeout_ms=_STATEMENT_TIMEOUT_MS
    )


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _assert_sanitized(raised: pytest.ExceptionInfo[Exception], *, port: int | None = None) -> None:
    message = str(raised.value)
    assert not [leak for leak in _FORBIDDEN if leak in message]
    if port is not None:
        assert str(port) not in message
    assert isinstance(raised.value.__cause__, psycopg.Error)


@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
def test_a_statement_cancelled_by_the_server_surfaces_the_stores_sanitized_error(
    provisioned_state: Provisioned, family: _Family
) -> None:
    config = _bounded(provisioned_state)

    with (
        _table_locked(provisioned_state, family.locked_table),
        pytest.raises(family.error) as raised,
    ):
        family.call(config)

    _assert_sanitized(raised)
    assert isinstance(raised.value.__cause__, psycopg.errors.QueryCanceled)


@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
def test_a_refused_connection_surfaces_the_stores_sanitized_error(
    provisioned_state: Provisioned, family: _Family
) -> None:
    port = _closed_port()
    config = dataclasses.replace(
        provisioned_state.state_config(), state_postgres_host="127.0.0.1", state_postgres_port=port
    )

    with pytest.raises(family.refused_error or family.error) as raised:
        family.call(config)

    _assert_sanitized(raised, port=port)


def test_a_cancelled_statement_marks_the_state_postgres_unhealthy_until_the_next_success(
    provisioned_state: Provisioned,
) -> None:
    config = _bounded(provisioned_state)
    store = PsycopgGraphLogStore(config)
    store.read_applied_position("compliance")
    assert is_healthy(STATE_POSTGRES)

    with (
        _table_locked(provisioned_state, "graph_log.applied_markers"),
        pytest.raises(GraphLogUnavailableError),
    ):
        store.read_applied_position("compliance")
    unhealthy_after_cancel = not is_healthy(STATE_POSTGRES)
    store.read_applied_position("compliance")

    assert unhealthy_after_cancel
    assert is_healthy(STATE_POSTGRES)
