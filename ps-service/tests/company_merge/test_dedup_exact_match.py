"""Tests for `ps_service.company_merge.dedup` (PLAN_REVIEWED.md §10
Increments 6-7): `read_existing_canonical_index` and `resolve_exact_match`.

Since issue #42 Company Merge dedupes Capability only -- Obligation is
Role-scoped and passed through, not deduped.
"""

from __future__ import annotations

from ps_service.company_merge.dedup import (
    read_existing_canonical_index,
    resolve_exact_match,
)
from ps_service.company_merge.models import ExistingCanonicalNode


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _ScriptedFakeGraph:
    """Satisfies `GraphHandle` structurally. Dispatches by a distinctive
    substring of the query text, mirroring
    `tests/company_merge/test_graph_reader.py`'s own `_ScriptedFakeGraph`
    dispatch style.
    """

    def __init__(self, *, capability_rows: list[object]) -> None:
        self._capability_rows = capability_rows

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "(n:Capability) RETURN" in q:
            return _FakeQueryResult(self._capability_rows)
        raise AssertionError(f"unexpected query issued: {q!r}")


def test_read_existing_canonical_index_returns_capability_nodes_with_and_without_embedding() -> (
    None
):
    graph = _ScriptedFakeGraph(
        capability_rows=[
            ["capability_1", "Risk Assessment Capability", [0.7, 0.8]],
            ["capability_2", "Incident Reporting Capability", [0.9, 1.0]],
            ["capability_3", "Vulnerability Handling Capability", None],
        ],
    )

    result = read_existing_canonical_index(graph, "Capability")

    assert result == (
        ExistingCanonicalNode(
            id="capability_1", text="Risk Assessment Capability", embedding=(0.7, 0.8)
        ),
        ExistingCanonicalNode(
            id="capability_2", text="Incident Reporting Capability", embedding=(0.9, 1.0)
        ),
        ExistingCanonicalNode(
            id="capability_3", text="Vulnerability Handling Capability", embedding=None
        ),
    )
    assert result[2].embedding is None


def test_read_existing_canonical_index_returns_empty_tuple_for_empty_graph() -> None:
    graph = _ScriptedFakeGraph(capability_rows=[])

    result = read_existing_canonical_index(graph, "Capability")

    assert result == ()


def test_resolve_exact_match_returns_true_when_incoming_id_is_present() -> None:
    assert resolve_exact_match("cap_1", frozenset({"cap_1", "cap_2"})) is True


def test_resolve_exact_match_returns_false_when_incoming_id_is_absent() -> None:
    assert resolve_exact_match("cap_3", frozenset({"cap_1", "cap_2"})) is False


def test_resolve_exact_match_returns_false_for_empty_existing_ids() -> None:
    assert resolve_exact_match("cap_1", frozenset()) is False
