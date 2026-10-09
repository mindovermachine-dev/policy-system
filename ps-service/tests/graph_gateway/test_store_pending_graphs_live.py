"""`postgres_live` test for `graphs_with_pending_entries` (issue #206, S13a; AC-BI-011).

Runs the read-only query as the real `ps_state` role against a scratch PS Postgres.

Deselected by default -- run with `uv run pytest -m postgres_live`; needs
`PS_TEST_POSTGRES_SUPERUSER_DSN` and `psql` on `PATH`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from ps_service.graph_gateway.models import GraphLogEntryDraft, GraphLogGroupDraft
from ps_service.graph_gateway.store import PsycopgGraphLogStore

if TYPE_CHECKING:
    from persistence.provisioned_postgres import Provisioned

pytestmark = pytest.mark.postgres_live


def _log(store: PsycopgGraphLogStore, count: int) -> str:
    graph = f"graph-{uuid.uuid4().hex[:8]}"
    entries = tuple(
        GraphLogEntryDraft(name="Capability", identity=f"cap-{index}", content={"n": index})
        for index in range(count)
    )
    store.append_group_standalone(GraphLogGroupDraft(graph=graph, entries=entries))
    return graph


def test_graphs_with_pending_entries_lists_exactly_the_graphs_behind_their_log(
    provisioned_graph_log: Provisioned,
) -> None:
    store = PsycopgGraphLogStore(provisioned_graph_log.state_config())
    unapplied = _log(store, 2)
    partly = _log(store, 3)
    done = _log(store, 1)
    store.advance_applied_position(partly, 2)
    store.advance_applied_position(done, 1)

    pending = store.graphs_with_pending_entries()

    assert unapplied in pending
    assert partly in pending
    assert done not in pending
    assert pending == tuple(sorted(pending))

    store.advance_applied_position(partly, 3)
    assert partly not in store.graphs_with_pending_entries()
