"""A statement failure in the pending-approval store surfaces as the store's own sanitized error.

The connection helper is replaced by one that raises the error the server sends when a statement
exceeds its timeout, with SQL text and a host in it. The store must map it to its existing error
type with a message free of that text, and keep the driver error as the cause.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

import psycopg.errors
import pytest

from ps_service.config import ServiceConfig
from ps_service.passkey_signing.errors import PendingApprovalPersistenceError
from ps_service.passkey_signing.store import PsycopgPendingApprovalStore

if TYPE_CHECKING:
    from collections.abc import Callable

_DRIVER_TEXT = (
    "canceling statement due to statement timeout\nCONTEXT: SELECT secret_sql FROM db.internal"
)
_LEAKS = ("SELECT", "secret_sql", "statement timeout", "db.internal", "canceling")

type _Call = Callable[[PsycopgPendingApprovalStore], object]

_CALLS: dict[str, _Call] = {
    "create": lambda store: store.create_pending_approval(
        tool_name="merge-capabilities",
        normalized_args={},
        actor_subject="subject-1",
        actor_issuer="https://issuer.example.com/",
        display_summary={},
    ),
    "get_by_id": lambda store: store.get_by_id("approval-1"),
    "get_by_code_hash": lambda store: store.get_by_code_hash(b"hash"),
    "mark_signed": lambda store: store.mark_signed("approval-1"),
    "set_outcome": lambda store: store.set_outcome("approval-1", {"status": "done"}),
}


def _timeout_instead_of_connecting(config: ServiceConfig, **_: object) -> NoReturn:
    del config
    raise psycopg.errors.QueryCanceled(_DRIVER_TEXT)


@pytest.mark.parametrize("call", list(_CALLS.values()), ids=list(_CALLS))
def test_a_cancelled_statement_surfaces_as_a_message_without_sql_or_host(
    monkeypatch: pytest.MonkeyPatch, call: _Call
) -> None:
    config = ServiceConfig(
        host="127.0.0.1", port=8000, graceful_shutdown_seconds=10, logging_dir=None
    )
    monkeypatch.setattr(
        "ps_service.passkey_signing.store.connect_from_config", _timeout_instead_of_connecting
    )
    store = PsycopgPendingApprovalStore(config)

    with pytest.raises(PendingApprovalPersistenceError) as raised:
        call(store)

    assert str(raised.value).startswith("failed to ")
    assert not [leak for leak in _LEAKS if leak in str(raised.value)]
    assert isinstance(raised.value.__cause__, psycopg.errors.QueryCanceled)
