"""`postgres_live` twin of the multi-graph group.

Runs the real store as the `ps_state` role
against a scratch PS Postgres: one transaction over several graphs, a real rollback when a later
leg fails inside the database, and two overlapping sets submitted from two connections.

Deselected by default -- run with `uv run pytest -m postgres_live ps-service/tests/graph_gateway`;
needs `PS_TEST_POSTGRES_SUPERUSER_DSN` and `psql` on `PATH`.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

from ps_service.graph_gateway.errors import GraphLogPersistenceError
from ps_service.graph_gateway.models import GraphLogEntryDraft, GraphLogGroupDraft
from ps_service.graph_gateway.store import PsycopgGraphLogStore

if TYPE_CHECKING:
    from persistence.provisioned_postgres import Provisioned

pytestmark = pytest.mark.postgres_live

_WORKERS = 4
_SETS_PER_WORKER = 5


def _graphs() -> tuple[str, str, str]:
    stem = uuid.uuid4().hex[:8]
    return (f"a-{stem}", f"b-{stem}", f"c-{stem}")


def _group(graph: str, count: int = 2) -> GraphLogGroupDraft:
    return GraphLogGroupDraft(
        graph=graph,
        entries=tuple(
            GraphLogEntryDraft(name="Capability", identity=f"{graph}-{i}", content={"n": i})
            for i in range(count)
        ),
    )


def _failing_group(graph: str) -> GraphLogGroupDraft:
    """Non-blank (passes validation) but Postgres text cannot hold NUL: a real mid-group failure."""
    return GraphLogGroupDraft(
        graph=graph,
        entries=(GraphLogEntryDraft(name="Capability\x00", identity="x", content={"k": 1}),),
    )


def test_three_legs_commit_in_one_transaction(
    provisioned_graph_log: Provisioned,
) -> None:
    store = PsycopgGraphLogStore(provisioned_graph_log.state_config())
    first, second, third = _graphs()

    appended = store.append_groups_standalone((_group(first, 3), _group(second, 1), _group(third)))

    assert [(g.graph, g.first_position, g.last_position) for g in appended] == [
        (first, 1, 3),
        (second, 1, 1),
        (third, 1, 2),
    ]
    assert [store.last_position(graph) for graph in (first, second, third)] == [3, 1, 2]


def test_a_failure_in_the_second_leg_rolls_back_the_first_leg_too(
    provisioned_graph_log: Provisioned,
) -> None:
    store = PsycopgGraphLogStore(provisioned_graph_log.state_config())
    first, second, third = _graphs()

    with pytest.raises(GraphLogPersistenceError):
        store.append_groups_standalone((_group(first), _failing_group(second), _group(third)))

    assert [store.last_position(graph) for graph in (first, second, third)] == [0, 0, 0]
    retried = store.append_groups_standalone((_group(first), _group(second)))
    assert [(g.first_position, g.last_position) for g in retried] == [(1, 2), (1, 2)]  # gap-free


def test_legs_out_of_ascending_order_or_repeating_a_graph_are_refused_before_any_sql(
    provisioned_graph_log: Provisioned,
) -> None:
    store = PsycopgGraphLogStore(provisioned_graph_log.state_config())
    first, second, _ = _graphs()

    with pytest.raises(ValueError, match="ascending"):
        store.append_groups_standalone((_group(second), _group(first)))
    with pytest.raises(ValueError, match="ascending"):
        store.append_groups_standalone((_group(first), _group(first)))


def test_overlapping_sets_from_several_connections_finish_with_gap_free_positions(
    provisioned_graph_log: Provisioned,
) -> None:
    store = PsycopgGraphLogStore(provisioned_graph_log.state_config())
    first, second, third = _graphs()
    overlapping = ((first, second), (second, third), (first, third), (first, second, third))

    def submit(worker: int) -> None:
        for count in range(_SETS_PER_WORKER):
            graphs = overlapping[(worker + count) % len(overlapping)]
            store.append_groups_standalone(tuple(_group(graph) for graph in graphs))

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        for future in [pool.submit(submit, worker) for worker in range(_WORKERS)]:
            future.result(timeout=60)  # a deadlock would time out here

    for graph in (first, second, third):
        positions = [entry.position for entry in store.read_entries(graph)]
        assert positions == list(range(1, len(positions) + 1))
