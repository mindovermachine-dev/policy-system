"""Capability candidate governance, obligation count and merge case (issue #190, slice 8).

The real reader + grouping run against a scripted single-tenant graph; only the
rows it returns are canned. Row shape: id, name, embedding, policy id, policy
title, policy status, obligation count.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from ps_service.graph_cleanup.service import find_capability_merge_candidates
from ps_service.logging import configure

if TYPE_CHECKING:
    from collections.abc import Sequence


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


@dataclass
class _Result:
    result_set: list[object]


class _Graph:
    def __init__(self, rows: Sequence[object]) -> None:
        self.rows: list[object] = list(rows)
        self.queries: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del params
        self.queries.append(q)
        return _Result(self.rows)


def _row(
    cap_id: str,
    name: str,
    policy: tuple[str, str, str] | None = None,
    obligations: int = 0,
) -> list[object]:
    policy_cells: list[object] = list(policy) if policy else [None, None, None]
    return [cap_id, name, None, *policy_cells, obligations]


_POLICY_A = ("pol_a", "Policy A", "approved")
_POLICY_B = ("pol_b", "Policy B", "draft")


def test_each_member_carries_obligation_count_and_governing_policy_or_none() -> None:
    graph = _Graph(
        [
            _row("cap_1", "Patch Management", _POLICY_A, 4),
            _row("cap_2", "patch-management", None, 2),
        ]
    )

    group = find_capability_merge_candidates(graph).groups[0]

    by_id = {member.id: member for member in group.members}
    assert by_id["cap_1"].obligation_count == 4
    governing = by_id["cap_1"].governing_policy
    assert governing is not None
    assert (governing.id, governing.title, governing.status) == _POLICY_A
    assert by_id["cap_2"].obligation_count == 2
    assert by_id["cap_2"].governing_policy is None


def test_no_member_governed_is_case_1() -> None:
    graph = _Graph([_row("cap_1", "A Duty"), _row("cap_2", "a-duty")])

    group = find_capability_merge_candidates(graph).groups[0]

    assert (group.merge_case, group.policies_distinct) == (1, False)


def test_exactly_one_member_governed_is_case_2() -> None:
    graph = _Graph([_row("cap_1", "A Duty", _POLICY_A), _row("cap_2", "a-duty")])

    group = find_capability_merge_candidates(graph).groups[0]

    assert (group.merge_case, group.policies_distinct) == (2, False)


def test_two_members_governed_by_the_same_policy_is_case_3_not_distinct() -> None:
    graph = _Graph([_row("cap_1", "A Duty", _POLICY_A), _row("cap_2", "a-duty", _POLICY_A)])

    group = find_capability_merge_candidates(graph).groups[0]

    assert (group.merge_case, group.policies_distinct) == (3, False)


def test_two_members_governed_by_different_policies_is_case_3_distinct() -> None:
    graph = _Graph([_row("cap_1", "A Duty", _POLICY_A), _row("cap_2", "a-duty", _POLICY_B)])

    group = find_capability_merge_candidates(graph).groups[0]

    assert (group.merge_case, group.policies_distinct) == (3, True)


def test_a_capability_returned_on_two_rows_is_reported_once() -> None:
    graph = _Graph(
        [
            _row("cap_1", "A Duty", _POLICY_A, 3),
            _row("cap_1", "A Duty", _POLICY_B, 3),
            _row("cap_2", "a-duty"),
        ]
    )

    group = find_capability_merge_candidates(graph).groups[0]

    assert [m.id for m in group.members] == ["cap_1", "cap_2"]


def test_the_read_follows_governed_by_and_requires_and_writes_nothing() -> None:
    graph = _Graph([])

    find_capability_merge_candidates(graph)

    query = graph.queries[0]
    assert "-[:GOVERNED_BY]->(p:Policy)" in query
    assert "(o:Obligation)-[:REQUIRES]->(c)" in query
    assert "count(DISTINCT o)" in query
    assert not any(kw in query for kw in ("MERGE ", "CREATE", "SET ", "DELETE"))
