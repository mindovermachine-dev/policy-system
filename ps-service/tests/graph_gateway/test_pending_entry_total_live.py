"""`postgres_live` test for `count_pending_entries`.

Runs the read-only total as the real `ps_state` role against a scratch PS Postgres.

Deselected by default -- run with `uv run pytest -m postgres_live`; needs
`PS_TEST_POSTGRES_SUPERUSER_DSN` and `psql` on `PATH`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from ps_service.graph_gateway.models import GraphLogEntryDraft, GraphLogGroupDraft
from ps_service.graph_gateway.pending_entries import count_pending_entries
from ps_service.graph_gateway.store import PsycopgGraphLogStore
from ps_service.persistence import connect_from_config

if TYPE_CHECKING:
    from persistence.provisioned_postgres import Provisioned

    from ps_service.config import ServiceConfig

pytestmark = pytest.mark.postgres_live


def _total(config: ServiceConfig) -> int:
    return count_pending_entries(config, connect_timeout_s=2.0, statement_timeout_ms=2000)


def _log(store: PsycopgGraphLogStore, count: int) -> str:
    graph = f"graph-{uuid.uuid4().hex[:8]}"
    entries = tuple(
        GraphLogEntryDraft(name="Capability", identity=f"cap-{index}", content={"n": index})
        for index in range(count)
    )
    store.append_group_standalone(GraphLogGroupDraft(graph=graph, entries=entries))
    return graph


def test_the_total_is_the_sum_of_unapplied_entries_across_graphs(
    provisioned_graph_log: Provisioned,
) -> None:
    config = provisioned_graph_log.state_config()
    store = PsycopgGraphLogStore(config)
    baseline = _total(config)
    never_applied = _log(store, 2)
    partly = _log(store, 5)
    done = _log(store, 3)
    store.advance_applied_position(partly, 2)
    store.advance_applied_position(done, 3)

    assert never_applied
    assert _total(config) == baseline + 2 + 3

    store.advance_applied_position(partly, 5)
    store.advance_applied_position(never_applied, 2)
    assert _total(config) == baseline


def test_a_graph_without_a_marker_counts_from_zero(provisioned_graph_log: Provisioned) -> None:
    config = provisioned_graph_log.state_config()
    store = PsycopgGraphLogStore(config)
    before = _total(config)

    _log(store, 4)

    assert _total(config) == before + 4


def test_the_total_is_never_negative_and_is_an_int(provisioned_graph_log: Provisioned) -> None:
    total = _total(provisioned_graph_log.state_config())

    assert isinstance(total, int)
    assert total >= 0


def test_the_connection_carries_the_requested_statement_timeout(
    provisioned_graph_log: Provisioned,
) -> None:
    with connect_from_config(
        provisioned_graph_log.state_config(), connect_timeout_s=2.0, statement_timeout_ms=1234
    ) as conn:
        row = conn.execute("SHOW statement_timeout").fetchone()

    assert row == ("1234ms",)
