"""The system-audit helper gives ungated callers an audit event id to attach to a group (#208).

Ingestion's internal path has no principal, but every `MutationGroup` needs the id of an
`audit_events` row (a foreign key). The helper inserts one row with a fixed system actor on its
own connection and returns the row's id. Postgres is the boundary, replaced by a hand-written
connection fake that records the statement it was given.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Self

import psycopg
import pytest

from ps_service.audit.errors import AuditTrailUnavailableError
from ps_service.graph_gateway.system_audit import (
    SYSTEM_ACTOR_ISSUER,
    SYSTEM_ACTOR_SUBJECT,
    record_system_audit,
)
from ps_service.persistence import StatePostgresConnectionError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType


class _FakeCursor:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, query: str, params: Mapping[str, object]) -> None:
        if self._connection.fail_execute is not None:
            raise self._connection.fail_execute
        self._connection.statements.append((query, dict(params)))

    def fetchone(self) -> tuple[uuid.UUID]:
        return (self._connection.row_id,)


class _FakeConnection:
    """Satisfies the slice of `psycopg.Connection` the helper uses."""

    def __init__(self, *, fail_execute: Exception | None = None) -> None:
        self.row_id = uuid.uuid4()
        self.fail_execute = fail_execute
        self.statements: list[tuple[str, dict[str, object]]] = []
        self.committed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.committed = True


def _record(connection: _FakeConnection) -> str:
    return record_system_audit(
        lambda: connection,  # pyright: ignore[reportArgumentType] -- structural stand-in
        action="ingestion_run.submit",
        resource_type="ingestion_run",
        resource_id="run-1",
        details={"status": "started", "trigger": "internal_seed"},
    )


def test_inserts_one_row_with_the_system_actor_and_returns_its_id() -> None:
    connection = _FakeConnection()

    audit_event_id = _record(connection)

    assert audit_event_id == str(connection.row_id)
    assert connection.committed
    ((query, params),) = connection.statements
    assert query.lstrip().startswith("INSERT INTO audit_events")
    assert params["actor_subject"] == SYSTEM_ACTOR_SUBJECT == "system:ingestion"
    assert params["actor_issuer"] == SYSTEM_ACTOR_ISSUER == "system"
    assert (params["action"], params["resource_type"], params["resource_id"]) == (
        "ingestion_run.submit",
        "ingestion_run",
        "run-1",
    )
    assert "'applied'" in query


def test_raises_audit_trail_unavailable_when_no_connection_can_be_opened() -> None:
    def refuse() -> psycopg.Connection[tuple[object, ...]]:
        message = "PS state Postgres is not configured"
        raise StatePostgresConnectionError(message)

    with pytest.raises(AuditTrailUnavailableError) as raised:
        record_system_audit(
            refuse,
            action="ingestion_run.submit",
            resource_type="ingestion_run",
            resource_id="run-1",
            details={},
        )

    assert "not configured" not in str(raised.value)
    assert isinstance(raised.value.__cause__, StatePostgresConnectionError)


def test_raises_audit_trail_unavailable_when_the_insert_fails() -> None:
    connection = _FakeConnection(fail_execute=psycopg.OperationalError("host=db password=hunter2"))

    with pytest.raises(AuditTrailUnavailableError) as raised:
        _record(connection)

    assert "hunter2" not in str(raised.value)
    assert not connection.committed
