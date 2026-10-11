"""`record_standalone_with_event_id` hands back the inserted row's id (issue #210, A6, F3).

The near-miss resolve anchors its graph group to the audit event it opened, so the opening row's
`INSERT ... RETURNING id` must reach the caller. `record_standalone` is unchanged for its many
other callers and now delegates to the new method. The state Postgres is the true boundary: a
hand-written connection stands in for it (`connect_from_config` is on the approved list); a
`postgres_live` twin in `test_store_record.py` covers the real `INSERT`.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Self

from ps_service.audit import AuditDetails, PsycopgAuditStore, register_audit_action
from ps_service.audit import store as audit_store_module
from ps_service.audit.store import EventIdAuditStore
from ps_service.config import ServiceConfig

if TYPE_CHECKING:
    from types import TracebackType

    import pytest

_ACTION = "test.event_id_probe"
_INSERTED_ID = "7b0f3c1e-5d0e-4f43-9b53-3f2a6f0a9c11"


class _ProbeDetails(AuditDetails):
    status: str


with contextlib.suppress(ValueError):  # already registered by an earlier import
    register_audit_action(_ACTION, _ProbeDetails)


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def execute(self, query: object, params: object = None) -> None:
        del query
        self._connection.executed.append(params)

    def fetchone(self) -> tuple[str] | None:
        return (self._connection.returned_id,) if self._connection.returned_id else None


class _Connection:
    """The slice of a psycopg connection `PsycopgAuditStore` uses; keeps what it was asked."""

    def __init__(self, returned_id: str | None = _INSERTED_ID) -> None:
        self.returned_id = returned_id
        self.executed: list[object] = []
        self.commits = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1


def _config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        is_local_test_bypass_active=True,
    )


def _store(monkeypatch: pytest.MonkeyPatch, connection: _Connection) -> PsycopgAuditStore:
    def _connect(_config: object) -> _Connection:
        return connection

    monkeypatch.setattr(audit_store_module, "connect_from_config", _connect)
    return PsycopgAuditStore(_config())


_ROW = {
    "actor_subject": "actor-sub",
    "actor_issuer": "https://issuer.example.com/",
    "action": _ACTION,
    "resource_type": "probe",
    "resource_id": "res-1",
    "outcome": "applied",
    "details": {"status": "started"},
}


def test_record_standalone_with_event_id_returns_the_inserted_rows_id_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    store = _store(monkeypatch, connection)

    event_id = store.record_standalone_with_event_id(**_ROW)  # pyright: ignore[reportArgumentType]

    assert event_id == _INSERTED_ID
    assert len(connection.executed) == 1
    assert connection.commits == 1


def test_record_standalone_still_returns_nothing_and_inserts_the_same_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    store = _store(monkeypatch, connection)

    result = store.record_standalone(**_ROW)  # pyright: ignore[reportArgumentType]

    assert result is None
    assert len(connection.executed) == 1
    assert connection.commits == 1


def test_the_postgres_store_satisfies_the_event_id_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert isinstance(_store(monkeypatch, _Connection()), EventIdAuditStore)
