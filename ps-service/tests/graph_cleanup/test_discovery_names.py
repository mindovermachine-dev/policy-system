"""Capability merge-candidate discovery, name basis (issue #190, slice 6, AC-BI-003 partial).

Detroit-style: the real reader + grouping run against a scripted single-tenant
graph (the approved FalkorDB boundary shape); only the rows it returns are canned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from ps_service.graph_cleanup.discovery import normalise_name
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
    """Answers the one active-capability read; records every query."""

    def __init__(self, rows: Sequence[object]) -> None:
        self.rows: list[object] = list(rows)
        self.queries: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del params
        self.queries.append(q)
        return _Result(self.rows)


def test_normalise_name_ignores_case_punctuation_and_spacing() -> None:
    assert (
        normalise_name("Vulnerability  Remediation-Process") == "vulnerability remediation process"
    )
    assert (
        normalise_name("  vulnerability_remediation process ")
        == "vulnerability remediation process"
    )


def test_variants_of_one_name_group_on_the_name_basis_and_unrelated_names_do_not() -> None:
    graph = _Graph(
        [
            ["cap_1", "Vulnerability Remediation Process", None, None, None, None, 0],
            ["cap_2", "vulnerability-remediation process", None, None, None, None, 0],
            ["cap_3", "VULNERABILITY REMEDIATION  PROCESS", None, None, None, None, 0],
            ["cap_4", "Incident Reporting", None, None, None, None, 0],
        ]
    )

    result = find_capability_merge_candidates(graph)

    assert len(result.groups) == 1
    group = result.groups[0]
    assert group.basis == "name"
    assert {member.id for member in group.members} == {"cap_1", "cap_2", "cap_3"}
    assert {member.name for member in group.members} >= {"Vulnerability Remediation Process"}


def test_no_duplicates_yields_no_groups() -> None:
    graph = _Graph(
        [["cap_1", "A", None, None, None, None, 0], ["cap_2", "B", None, None, None, None, 0]]
    )

    assert find_capability_merge_candidates(graph).groups == ()


def test_the_read_excludes_merged_tombstones_and_includes_null_status() -> None:
    graph = _Graph([])

    find_capability_merge_candidates(graph)

    assert len(graph.queries) == 1
    query = graph.queries[0]
    assert "coalesce(c.status,'active') = 'active'" in query
    assert not any(kw in query for kw in ("MERGE ", "CREATE", "SET ", "DELETE"))


def test_groups_and_members_are_ordered_deterministically() -> None:
    graph = _Graph(
        [
            ["cap_b", "Zeta Duty", None, None, None, None, 0],
            ["cap_a", "zeta-duty", None, None, None, None, 0],
            ["cap_d", "alpha duty", None, None, None, None, 0],
            ["cap_c", "Alpha Duty", None, None, None, None, 0],
        ]
    )

    result = find_capability_merge_candidates(graph)

    assert [[m.id for m in g.members] for g in result.groups] == [
        ["cap_a", "cap_b"],
        ["cap_c", "cap_d"],
    ]
