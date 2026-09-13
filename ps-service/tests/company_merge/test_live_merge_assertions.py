"""Tests for the 3 NEW snapshot functions in `_live_merge_assertions.py`
(issue #28, CHANGES.md #1): `_snapshot_node_properties`/
`_snapshot_edge_properties`/`_snapshot_edge_ids`.

The other 7 functions in that module are extracted VERBATIM out of
`test_live_capstone.py` and already have coverage there, indirectly, via
that file's own existing assertions on them -- not re-tested here.

Fakes implement the `GraphHandle`/`GraphQueryResult` Protocols
(`ps_service.company_merge.falkordb_client`) structurally -- no mocking
library, matching L2 Testing Patterns' "mock at component boundaries" and
this repo's existing `company_merge` test convention (see e.g.
`test_graph_writer_passthrough.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from company_merge._live_merge_assertions import (
    _snapshot_edge_ids,  # pyright: ignore[reportPrivateUsage]
    _snapshot_edge_properties,  # pyright: ignore[reportPrivateUsage]
    _snapshot_node_properties,  # pyright: ignore[reportPrivateUsage]
)


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


@dataclass
class _FakeGraph:
    """Satisfies `GraphHandle` structurally.

    Dispatches by a substring of the issued Cypher (e.g. `"n:Capability"`,
    `"e:DEFINES"`, `":HAS]"`) to a caller-configured row set; any query with
    no matching substring returns zero rows -- matches the real "no nodes of
    this label yet" / "no edges of this type yet" shape rather than raising.
    """

    rows_by_query_substring: dict[str, list[object]] = field(default_factory=dict)
    calls: list[_RecordedCall] = field(default_factory=list)

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        for substring, rows in self.rows_by_query_substring.items():
            if substring in q:
                return _FakeQueryResult(rows)
        return _FakeQueryResult([])


def test_snapshot_node_properties_strips_embedding_from_capability() -> None:
    graph = _FakeGraph(
        rows_by_query_substring={
            "n:Capability": [
                [
                    "cap_incident_notification",
                    {
                        "name": "Incident Notification",
                        "confidence": 0.9,
                        "embedding": [0.1, 0.2, 0.3],
                    },
                ],
            ],
        }
    )

    snapshot = _snapshot_node_properties(graph)

    assert snapshot["Capability"] == {
        "cap_incident_notification": {"name": "Incident Notification", "confidence": 0.9},
    }
    # every other node label queried too, even with zero rows.
    assert snapshot["RegulatoryInstrument"] == {}
    assert snapshot["Role"] == {}
    assert snapshot["Requirement"] == {}
    assert snapshot["Obligation"] == {}


def test_snapshot_node_properties_does_not_strip_embedding_from_non_capability() -> None:
    # Only Capability's embedding is stripped (CHANGES.md #1) -- an
    # Obligation carrying a stray "embedding"-named property (not a real
    # shape, but the stripping must be Capability-specific, not blanket)
    # passes through unchanged.
    graph = _FakeGraph(
        rows_by_query_substring={
            "n:Obligation": [["obl_x", {"text": "Do the thing", "embedding": [0.4]}]],
        }
    )

    snapshot = _snapshot_node_properties(graph)

    assert snapshot["Obligation"] == {"obl_x": {"text": "Do the thing", "embedding": [0.4]}}


def test_snapshot_edge_properties_returns_properties_for_defines_edge() -> None:
    graph = _FakeGraph(
        rows_by_query_substring={
            "e:DEFINES": [["CRA-1.0", "role_manufacturer_abc123", {"source_ref": "Art. 13(1)"}]],
        }
    )

    snapshot = _snapshot_edge_properties(graph)

    assert snapshot["DEFINES"] == {
        "CRA-1.0|role_manufacturer_abc123": {"source_ref": "Art. 13(1)"},
    }
    # EXPRESSES is the other property-bearing type -- queried too, zero rows here.
    assert snapshot["EXPRESSES"] == {}
    # HAS/SATISFIED_BY/REQUIRES never carry properties -- not part of this snapshot at all.
    assert set(snapshot) == {"DEFINES", "EXPRESSES"}


def test_snapshot_edge_ids_returns_existence_pairs_for_has_edge_ignoring_properties() -> None:
    graph = _FakeGraph(
        rows_by_query_substring={
            ":HAS]": [["role_manufacturer_abc123", "obl_report_incident_xyz"]],
        }
    )

    snapshot = _snapshot_edge_ids(graph)

    assert snapshot["HAS"] == [["role_manufacturer_abc123", "obl_report_incident_xyz"]]
    # SATISFIED_BY/REQUIRES are the other propertyless types -- queried too, zero rows here.
    assert snapshot["SATISFIED_BY"] == []
    assert snapshot["REQUIRES"] == []
    # DEFINES/EXPRESSES are property-bearing -- not part of this existence-only snapshot.
    assert set(snapshot) == {"HAS", "SATISFIED_BY", "REQUIRES"}

    # The query issued for HAS never asks for edge properties at all (existence-survival
    # only) -- confirms "ignoring any (nonexistent) properties" structurally, not just by
    # the fake's return shape.
    has_calls = [call for call in graph.calls if ":HAS]" in call.query]
    assert len(has_calls) == 1
    assert "properties" not in has_calls[0].query
