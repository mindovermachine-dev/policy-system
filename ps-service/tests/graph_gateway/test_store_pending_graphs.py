"""`graphs_with_pending_entries` on the in-memory log store (issue #206, S13a).

The read-only question startup recovery asks: which graphs have logged entries beyond their
applied marker? The `postgres_live` twin in `test_store_pending_graphs_live.py` proves the same
answers against the real store.
"""

from __future__ import annotations

import pytest

from graph_gateway._fakes import InMemoryGraphLogStore
from ps_service.graph_gateway.errors import GraphLogUnavailableError
from ps_service.graph_gateway.models import GraphLogEntryDraft, GraphLogGroupDraft


def _log(store: InMemoryGraphLogStore, graph: str, count: int) -> None:
    entries = tuple(
        GraphLogEntryDraft(name="Capability", identity=f"cap-{index}", content={"n": index})
        for index in range(count)
    )
    store.append_group_standalone(GraphLogGroupDraft(graph=graph, entries=entries))


def test_an_empty_log_has_no_graph_with_pending_entries() -> None:
    assert InMemoryGraphLogStore().graphs_with_pending_entries() == ()


def test_a_graph_with_no_marker_and_logged_entries_is_pending() -> None:
    store = InMemoryGraphLogStore()
    _log(store, "compliance", 2)

    assert store.graphs_with_pending_entries() == ("compliance",)


def test_a_graph_whose_marker_trails_the_log_is_pending_until_the_marker_catches_up() -> None:
    store = InMemoryGraphLogStore()
    _log(store, "compliance", 3)
    store.advance_applied_position("compliance", 2)
    assert store.graphs_with_pending_entries() == ("compliance",)

    store.advance_applied_position("compliance", 3)

    assert store.graphs_with_pending_entries() == ()


def test_only_lagging_graphs_are_listed_and_in_name_order() -> None:
    store = InMemoryGraphLogStore()
    for graph in ("zeta", "alpha", "done"):
        _log(store, graph, 1)
    store.advance_applied_position("done", 1)

    assert store.graphs_with_pending_entries() == ("alpha", "zeta")


def test_an_unreadable_log_raises_rather_than_reporting_nothing_pending() -> None:
    store = InMemoryGraphLogStore()
    store.fail_reads()

    with pytest.raises(GraphLogUnavailableError):
        store.graphs_with_pending_entries()
