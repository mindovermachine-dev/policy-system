"""Capability candidate discovery, embedding basis (issue #190, slice 7, AC-BI-003 partial).

Cached-embedding cosine only; discovery never calls an LLM or embedding model.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import ps_service.graph_cleanup as graph_cleanup_package
from ps_service.graph_cleanup.discovery import (
    DEFAULT_CAPABILITY_MIN_SIMILARITY,
    resolve_min_similarity,
)
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


def test_default_threshold_is_the_recorded_constant() -> None:
    assert DEFAULT_CAPABILITY_MIN_SIMILARITY == 0.90


def test_explicit_beats_configured_beats_default() -> None:
    assert resolve_min_similarity(0.95, 0.83) == 0.95
    assert resolve_min_similarity(None, 0.83) == 0.83
    assert resolve_min_similarity(None, None) == DEFAULT_CAPABILITY_MIN_SIMILARITY


def test_pair_above_the_threshold_groups_with_basis_embedding() -> None:
    graph = _Graph(
        [
            ["cap_a", "Patch Management", [1.0, 0.0], None, None, None, 0],
            ["cap_b", "Software Updates", [0.99, 0.05], None, None, None, 0],
            ["cap_c", "Incident Reporting", [0.0, 1.0], None, None, None, 0],
        ]
    )

    result = find_capability_merge_candidates(graph, min_similarity=0.9)

    assert [(g.basis, [m.id for m in g.members]) for g in result.groups] == [
        ("embedding", ["cap_a", "cap_b"])
    ]


def test_pair_below_the_threshold_does_not_group() -> None:
    graph = _Graph(
        [
            ["cap_a", "A", [1.0, 0.0], None, None, None, 0],
            ["cap_b", "B", [0.8, 0.6], None, None, None, 0],
        ]
    )

    assert find_capability_merge_candidates(graph, min_similarity=0.9).groups == ()


def test_similar_vectors_link_transitively_into_one_group() -> None:
    graph = _Graph(
        [
            ["cap_a", "A", [1.0, 0.0], None, None, None, 0],
            ["cap_b", "B", [0.95, 0.312], None, None, None, 0],
            ["cap_c", "C", [0.81, 0.586], None, None, None, 0],
        ]
    )

    groups = find_capability_merge_candidates(graph, min_similarity=0.9).groups

    assert [[m.id for m in g.members] for g in groups] == [["cap_a", "cap_b", "cap_c"]]


def test_a_node_without_a_cached_embedding_is_found_by_name_only() -> None:
    graph = _Graph(
        [
            ["cap_a", "Patch Management", None, None, None, None, 0],
            ["cap_b", "patch-management", None, None, None, None, 0],
            ["cap_c", "Unrelated", None, None, None, None, 0],
            ["cap_d", "Also Unrelated", [1.0, 0.0], None, None, None, 0],
        ]
    )

    groups = find_capability_merge_candidates(graph, min_similarity=0.9).groups

    assert [(g.basis, [m.id for m in g.members]) for g in groups] == [("name", ["cap_a", "cap_b"])]


def test_name_equal_pair_stays_basis_name_even_when_their_vectors_also_match() -> None:
    graph = _Graph(
        [
            ["cap_a", "Patch Mgmt", [1.0, 0.0], None, None, None, 0],
            ["cap_b", "patch-mgmt", [1.0, 0.0], None, None, None, 0],
        ]
    )

    groups = find_capability_merge_candidates(graph, min_similarity=0.9).groups

    assert [g.basis for g in groups] == ["name"]


def test_unscorable_vectors_are_skipped_without_failing_discovery() -> None:
    graph = _Graph(
        [
            ["cap_a", "A", [1.0, 0.0], None, None, None, 0],
            ["cap_b", "B", [1.0, 0.0, 0.0], None, None, None, 0],
            ["cap_c", "C", [0.0, 0.0], None, None, None, 0],
        ]
    )

    assert find_capability_merge_candidates(graph, min_similarity=0.9).groups == ()


def test_the_read_selects_the_cached_embedding_and_writes_nothing() -> None:
    graph = _Graph([])

    find_capability_merge_candidates(graph)

    assert "c.embedding" in graph.queries[0]
    assert not any(kw in graph.queries[0] for kw in ("MERGE ", "CREATE", "SET ", "DELETE"))


def test_graph_cleanup_never_imports_an_llm_or_embedding_router() -> None:
    """AC-style proof: the only cross-component import is the pure `cosine_similarity`."""
    package_dir = Path(graph_cleanup_package.__file__).parent
    imported: set[str] = set()
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

    assert not any("llm_interface" in name or "route_embedding" in name for name in imported)
    company_merge_imports = {n for n in imported if n.startswith("ps_service.company_merge")}
    assert {n for n in company_merge_imports if not n.endswith(("GraphHandle",))} <= {
        "ps_service.company_merge.similarity.cosine_similarity",
        "ps_service.company_merge.falkordb_client.connect_from_config",
        "ps_service.company_merge.falkordb_client.select_graph",
        "ps_service.company_merge.falkordb_client.single_tenant_graph_name",
        "ps_service.company_merge.falkordb_client.GraphHandle",
        "ps_service.company_merge.errors.CompanyMergeValidationError",
    }
