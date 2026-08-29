"""Tests for `ps_service.change_monitor.graph_reader` (AC-002, AC-003).

Test 4 (PLAN_REVIEWED.md §3): scripted `FakeGraph` rows map to
`TrackedInstrumentNode`s carrying only the projected fields, and the
enumeration query carries the `active` / `external` /
`regulation`-or-`directive` filter and the `n.celex` / `n.effective_date`
projections verbatim -- so a `national_transposition`, internal, or
superseded row can never be returned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from change_monitor._fakes import FakeGraph, FakeQueryResult
from ps_service.change_monitor.graph_reader import read_tracked_instruments
from ps_service.change_monitor.models import TrackedInstrumentNode

if TYPE_CHECKING:
    from ps_service.change_monitor.falkordb_client import GraphHandle


def _as_handle(graph: FakeGraph) -> GraphHandle:
    """Hand the structural fake to the reader as the `GraphHandle` it expects."""
    return graph


def test_rows_map_to_tracked_instrument_nodes_with_only_projected_fields() -> None:
    graph = FakeGraph(
        [
            FakeQueryResult(
                [
                    ["CRA-1.0", "32024R2847", "regulation", "2027-12-11"],
                    ["NIS2-1.0", "32022L2555", "directive", "2024-10-17"],
                ]
            )
        ]
    )

    nodes = read_tracked_instruments(_as_handle(graph))

    assert nodes == (
        TrackedInstrumentNode(
            regulatory_instrument_id="CRA-1.0",
            celex="32024R2847",
            instrument_type="regulation",
            effective_date="2027-12-11",
        ),
        TrackedInstrumentNode(
            regulatory_instrument_id="NIS2-1.0",
            celex="32022L2555",
            instrument_type="directive",
            effective_date="2024-10-17",
        ),
    )


def test_null_celex_and_null_effective_date_are_tolerated() -> None:
    graph = FakeGraph([FakeQueryResult([["LEGACY-1.0", None, "regulation", None]])])

    (node,) = read_tracked_instruments(_as_handle(graph))

    assert node.celex is None
    assert node.effective_date == ""


def test_empty_graph_yields_empty_tuple() -> None:
    graph = FakeGraph([FakeQueryResult([])])

    assert read_tracked_instruments(_as_handle(graph)) == ()


def test_enumeration_query_carries_the_filter_and_projections_verbatim() -> None:
    graph = FakeGraph([FakeQueryResult([])])

    read_tracked_instruments(_as_handle(graph))

    assert len(graph.calls) == 1
    query = graph.calls[0].query
    assert graph.calls[0].params is None
    assert "n.status = 'active'" in query
    assert "n.source_type = 'external'" in query
    assert "n.instrument_type IN ['regulation', 'directive']" in query
    assert "n.celex" in query
    assert "n.effective_date" in query


def test_read_issues_no_write() -> None:
    graph = FakeGraph([FakeQueryResult([])])

    read_tracked_instruments(_as_handle(graph))

    assert graph.writes == []
