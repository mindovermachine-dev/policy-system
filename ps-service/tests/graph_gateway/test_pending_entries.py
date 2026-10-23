"""`count_pending_entries`: the read-only source behind the pending graph-log gauge.

No Postgres is needed here: the query text is checked as written, and failure paths use an
unconfigured store, a closed loopback port and a listener that accepts but never answers.
The query itself runs against a real Postgres in `test_pending_entry_total_live.py`.
"""

from __future__ import annotations

import socket
import time
from typing import TYPE_CHECKING

import psycopg
import pytest

from ps_service.config import ServiceConfig
from ps_service.dependency_health import STATE_POSTGRES, is_healthy, mark_healthy
from ps_service.graph_gateway.pending_entries import PENDING_ENTRIES_SQL, count_pending_entries
from ps_service.persistence import StatePostgresConnectionError

if TYPE_CHECKING:
    from collections.abc import Iterator


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


def test_the_query_is_one_parameterless_total_over_the_graph_log_tables() -> None:
    sql = PENDING_ENTRIES_SQL

    assert "graph_log.entries" in sql
    assert "graph_log.applied_markers" in sql
    assert "max(position)" in sql
    assert "coalesce" in sql
    assert "%s" not in sql
    assert "%(" not in sql


def test_the_query_returns_no_graph_column() -> None:
    select_list = PENDING_ENTRIES_SQL.split("FROM", 1)[0].lower()

    assert "sum(" in select_list
    assert "logged.graph" not in select_list


def test_an_unconfigured_store_raises_and_leaves_dependency_health_alone() -> None:
    mark_healthy(STATE_POSTGRES)

    with pytest.raises(StatePostgresConnectionError):
        count_pending_entries(_config(host=None), connect_timeout_s=1.0, statement_timeout_ms=500)

    assert is_healthy(STATE_POSTGRES) is True


def test_an_unreachable_store_raises_and_leaves_dependency_health_alone() -> None:
    mark_healthy(STATE_POSTGRES)

    with pytest.raises(psycopg.OperationalError):
        count_pending_entries(
            _config(host="127.0.0.1", port=59999), connect_timeout_s=1.0, statement_timeout_ms=500
        )

    assert is_healthy(STATE_POSTGRES) is True


@pytest.fixture
def silent_listener() -> Iterator[int]:
    """A loopback port that completes the TCP handshake but never speaks Postgres."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        yield listener.getsockname()[1]


def test_a_store_that_never_answers_is_abandoned_at_the_connect_timeout(
    silent_listener: int,
) -> None:
    started = time.monotonic()

    with pytest.raises(psycopg.OperationalError):
        count_pending_entries(
            _config(host="127.0.0.1", port=silent_listener),
            connect_timeout_s=1.0,
            statement_timeout_ms=500,
        )

    assert time.monotonic() - started < 4.0
    assert is_healthy(STATE_POSTGRES) is True
