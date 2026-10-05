"""Duplicate-obligation discovery within one Role (issue #190, slice 9, AC-BI-004).

The real reader + grouping run against a scripted single-tenant graph; only the
rows it returns are canned. Row shape: role id, role name, obligation id,
obligation text, requirement id, requirement `source_ref` (via `EXPRESSES`).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import ps_service.graph_cleanup as graph_cleanup_package
from ps_service.graph_cleanup.discovery import (
    OBLIGATION_NEAR_TEXT_MIN_JACCARD,
    group_duplicate_obligations,
)
from ps_service.graph_cleanup.service import find_duplicate_obligations
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
        self.queries: list[tuple[str, dict[str, object] | None]] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        self.queries.append((q, params))
        return _Result(self.rows)


def _row(role: str, obl: str, text: str, req: str | None, ref: str | None) -> list[object]:
    return [role, f"Name of {role}", obl, text, req, ref]


def test_identical_text_under_the_same_role_groups_with_requirement_source_refs() -> None:
    graph = _Graph(
        [
            _row("role_m", "obl_1", "Notify the authority of incidents", "R_6.1", "Art. 6(1)"),
            _row("role_m", "obl_2", "notify the authority of  incidents.", "R_6.2", "Art. 6(2)"),
            _row("role_m", "obl_2", "notify the authority of  incidents.", "R_14.1", "Art. 14(1)"),
            _row("role_m", "obl_3", "Keep an SBOM", "R_13.1", "Art. 13(1)"),
        ]
    )

    result = find_duplicate_obligations(graph)

    assert len(result.groups) == 1
    group = result.groups[0]
    assert (group.role_id, group.role_name, group.basis) == (
        "role_m",
        "Name of role_m",
        "identical_text",
    )
    assert [m.id for m in group.members] == ["obl_1", "obl_2"]
    refs = {m.id: [r.source_ref for r in m.requirements] for m in group.members}
    assert refs == {"obl_1": ["Art. 6(1)"], "obl_2": ["Art. 14(1)", "Art. 6(2)"]}
    assert [r.requirement_id for r in group.members[1].requirements] == ["R_14.1", "R_6.2"]


def test_identical_text_under_two_roles_is_never_grouped() -> None:
    graph = _Graph(
        [
            _row("role_importer", "obl_a", "Notify the authority", "R_a", "Art. 21"),
            _row("role_manufacturer", "obl_b", "Notify the authority", "R_b", "Art. 14"),
        ]
    )

    assert find_duplicate_obligations(graph).groups == ()


def test_near_identical_text_groups_with_basis_near_text() -> None:
    graph = _Graph(
        [
            _row("role_m", "obl_1", "Handle vulnerabilities in the product effectively", "R1", "A"),
            _row("role_m", "obl_2", "Handle vulnerabilities in the product", "R2", "B"),
            _row("role_m", "obl_3", "Appoint a representative", "R3", "C"),
        ]
    )

    groups = find_duplicate_obligations(graph).groups

    assert [(g.basis, [m.id for m in g.members]) for g in groups] == [
        ("near_text", ["obl_1", "obl_2"])
    ]


def test_text_below_the_near_threshold_is_not_grouped() -> None:
    assert 0.5 < OBLIGATION_NEAR_TEXT_MIN_JACCARD <= 1.0
    graph = _Graph(
        [
            _row("role_m", "obl_1", "Report actively exploited vulnerabilities", "R1", "A"),
            _row("role_m", "obl_2", "Report severe incidents", "R2", "B"),
        ]
    )

    assert find_duplicate_obligations(graph).groups == ()


def test_near_text_never_crosses_roles() -> None:
    graph = _Graph(
        [
            _row("role_a", "obl_1", "Handle vulnerabilities in the product effectively", "R1", "A"),
            _row("role_b", "obl_2", "Handle vulnerabilities in the product", "R2", "B"),
        ]
    )

    assert find_duplicate_obligations(graph).groups == ()


def test_an_obligation_without_requirements_is_listed_with_none() -> None:
    graph = _Graph(
        [
            _row("role_m", "obl_1", "Do the thing", None, None),
            _row("role_m", "obl_2", "do the thing", "R2", "Art. 2"),
        ]
    )

    members = find_duplicate_obligations(graph).groups[0].members

    assert members[0].requirements == ()
    assert [r.source_ref for r in members[1].requirements] == ["Art. 2"]


def test_groups_are_ordered_deterministically_by_role_then_first_member() -> None:
    rows = [
        _row("role_b", "obl_4", "Beta duty", "R4", "x"),
        _row("role_b", "obl_3", "beta duty", "R3", "x"),
        _row("role_a", "obl_2", "Alpha duty", "R2", "x"),
        _row("role_a", "obl_1", "alpha duty", "R1", "x"),
    ]

    groups = find_duplicate_obligations(_Graph(rows)).groups

    assert [(g.role_id, [m.id for m in g.members]) for g in groups] == [
        ("role_a", ["obl_1", "obl_2"]),
        ("role_b", ["obl_3", "obl_4"]),
    ]


def test_role_filter_is_passed_as_a_parameter_and_the_read_writes_nothing() -> None:
    graph = _Graph([])

    find_duplicate_obligations(graph, role_id="role_m")
    find_duplicate_obligations(graph)

    (query, params), (_, unfiltered) = graph.queries
    assert params == {"role_id": "role_m"}
    assert unfiltered == {"role_id": None}
    assert "(r:Role)-[:HAS]->(o:Obligation)" in query
    assert "SATISFIED_BY" in query
    assert "EXPRESSES" in query
    assert not any(kw in query for kw in ("MERGE ", "CREATE", "SET ", "DELETE"))


def test_the_pure_grouper_handles_an_empty_input() -> None:
    assert group_duplicate_obligations(()) == ()


def test_discovery_still_imports_no_llm_router() -> None:
    package_dir = Path(graph_cleanup_package.__file__).parent
    imported: set[str] = set()
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)

    assert not any("llm_interface" in name or "route_embedding" in name for name in imported)
