"""Tests for `ps_service.company_merge.preconditions` (Issue #28 slice 1,
AC-BI-002/AC-BI-003).

Hand-written fake `GraphHandle`/`GraphQueryResult`, no live dependency --
mirrors `tests/company_merge/test_graph_reader.py`'s `_FakeQueryResult`
style. `check_obligation_has_edge_cardinality` issues exactly one query, so
the fake graph here simply answers whatever `.query()` call it receives with
a fixed, scripted row set.
"""

from __future__ import annotations

from ps_service.company_merge.preconditions import (
    ObligationHasEdgeViolation,
    check_obligation_has_edge_cardinality,
)


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeGraph:
    """Satisfies `GraphHandle` structurally.

    Returns a fixed, scripted row set for the one query
    `check_obligation_has_edge_cardinality` issues -- no dispatch by query
    text needed since only one query is ever sent.
    """

    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        del q, params
        return _FakeQueryResult(self._rows)


def test_returns_empty_tuple_when_every_obligation_has_exactly_one_has_edge() -> None:
    graph = _FakeGraph([["obligation-1", 1], ["obligation-2", 1]])

    violations = check_obligation_has_edge_cardinality(graph)

    assert violations == ()


def test_returns_one_violation_when_one_obligation_has_zero_has_edges() -> None:
    graph = _FakeGraph([["obligation-1", 1], ["obligation-2", 0]])

    violations = check_obligation_has_edge_cardinality(graph)

    assert violations == (
        ObligationHasEdgeViolation(obligation_id="obligation-2", has_edge_count=0),
    )


def test_returns_one_violation_when_one_obligation_has_two_has_edges() -> None:
    """The exact shape #34 hit: a live-capstone re-run against an
    already-populated baseline left one Obligation with 2 `HAS` edges.
    """
    graph = _FakeGraph([["obligation-1", 1], ["obligation-2", 2]])

    violations = check_obligation_has_edge_cardinality(graph)

    assert violations == (
        ObligationHasEdgeViolation(obligation_id="obligation-2", has_edge_count=2),
    )


def test_returns_empty_tuple_for_empty_graph_with_zero_obligations() -> None:
    graph = _FakeGraph([])

    violations = check_obligation_has_edge_cardinality(graph)

    assert violations == ()


def test_returns_only_the_violating_obligation_in_a_mixed_clean_and_violating_set() -> None:
    graph = _FakeGraph(
        [
            ["obligation-1", 1],
            ["obligation-2", 1],
            ["obligation-3", 1],
            ["obligation-4", 0],
        ]
    )

    violations = check_obligation_has_edge_cardinality(graph)

    assert violations == (
        ObligationHasEdgeViolation(obligation_id="obligation-4", has_edge_count=0),
    )
