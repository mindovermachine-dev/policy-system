"""`postgres_live`: the checkpoint range read the replay uses (#207 S7L).

Replay verifies against the highest checkpoint at or below the log's head, so the store must be
able to answer "the highest checkpoint at or below this position" in one query.

Deselected by default -- run with `uv run pytest -m postgres_live`.
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


def _store(prov: Provisioned) -> PsycopgGraphLogStore:
    return PsycopgGraphLogStore(prov.state_config())


def _logged_graph(store: PsycopgGraphLogStore, entry_count: int) -> str:
    graph = f"graph-{uuid.uuid4().hex[:8]}"
    entries = tuple(
        GraphLogEntryDraft(name="Capability", identity=f"cap-{index}", content={"n": index})
        for index in range(entry_count)
    )
    store.append_group_standalone(GraphLogGroupDraft(graph=graph, entries=entries))
    return graph


def test_no_checkpoint_at_or_below_a_position_reads_as_none(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    graph = _logged_graph(store, 5)
    store.record_digest_checkpoint(graph, 4, "sha256:" + "aa" * 32)

    assert store.read_highest_checkpoint_at_or_below(graph, 3) is None
    assert store.read_highest_checkpoint_at_or_below(f"other-{graph}", 5) is None


def test_the_checkpoint_at_exactly_the_position_is_returned(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    graph = _logged_graph(store, 5)
    store.record_digest_checkpoint(graph, 3, "sha256:" + "aa" * 32)

    found = store.read_highest_checkpoint_at_or_below(graph, 3)

    assert found is not None
    assert (found.graph, found.position, found.canonical_digest) == (
        graph,
        3,
        "sha256:" + "aa" * 32,
    )


def test_the_highest_checkpoint_below_the_position_is_returned(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    graph = _logged_graph(store, 9)
    store.record_digest_checkpoint(graph, 3, "sha256:" + "03" * 32)
    store.record_digest_checkpoint(graph, 7, "sha256:" + "07" * 32)
    store.record_digest_checkpoint(graph, 9, "sha256:" + "09" * 32)

    found = store.read_highest_checkpoint_at_or_below(graph, 8)

    assert found is not None
    assert (found.position, found.canonical_digest) == (7, "sha256:" + "07" * 32)


def test_read_entries_limit_pages_in_position_order(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    graph = _logged_graph(store, 7)

    first = store.read_entries(graph, limit=3)
    second = store.read_entries(graph, after_position=first[-1].position, limit=3)
    rest = store.read_entries(graph, after_position=second[-1].position, limit=3)

    assert [e.position for e in first] == [1, 2, 3]
    assert [e.position for e in second] == [4, 5, 6]
    assert [e.position for e in rest] == [7]
    assert [e.position for e in store.read_entries(graph)] == list(range(1, 8))


def test_logged_graphs_lists_every_graph_with_entries_in_name_order(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    second = _logged_graph(store, 2)
    first = _logged_graph(store, 3)

    listed = store.logged_graphs()

    assert listed == tuple(sorted(listed))  # the log is append-only: other tests' graphs remain
    assert {first, second} <= set(listed)
