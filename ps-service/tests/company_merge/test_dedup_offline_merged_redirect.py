"""Offline exact-key redirect over `merged` Capability tombstones (issue #190, AC-BI-008/009).

Offline twin of `test_dedup_merged_redirect.py`: `resolve_capability_convergence_offline`
follows `MERGED_INTO`, never scores a tombstone, and does not count a tombstone in
`skipped_missing_embedding_count`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ps_service.company_merge.dedup import resolve_capability_convergence_offline
from ps_service.company_merge.errors import CompanyMergeValidationError
from ps_service.company_merge.models import BaselineNode

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines

_THRESHOLD = 0.85


class _FakeQueryResult:
    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeGraph:
    def __init__(self, *, capability_rows: list[object], merged_rows: list[object]) -> None:
        self._capability_rows = capability_rows
        self._merged_rows = merged_rows
        self.queries: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.queries.append(q)
        if "MERGED_INTO" in q:
            return _FakeQueryResult(self._merged_rows)
        if "(n:Capability) RETURN" in q:
            return _FakeQueryResult(self._capability_rows)
        raise AssertionError(f"unexpected query issued: {q!r}")


def _node(node_id: str, name: str) -> BaselineNode:
    return BaselineNode(id=node_id, properties={"name": name, "confidence": 0.9})


def test_offline_tombstone_id_resolves_to_survivor() -> None:
    graph = _FakeGraph(
        capability_rows=[["cap_x", "X", [1.0, 0.0]], ["cap_y", "Y", [0.0, 1.0]]],
        merged_rows=[["cap_x", "cap_y"]],
    )

    result = resolve_capability_convergence_offline(
        (_node("cap_x", "X"),),
        incoming_embeddings={"cap_x": (1.0, 0.0)},
        single_tenant_graph=graph,
        threshold=_THRESHOLD,
    )

    assert [(r.incoming_id, r.canonical_id, r.match_kind) for r in result.resolutions] == [
        ("cap_x", "cap_y", "redirected")
    ]


def test_offline_tombstone_is_not_a_semantic_candidate() -> None:
    graph = _FakeGraph(
        capability_rows=[["cap_x", "X", [1.0, 0.0]]], merged_rows=[["cap_x", "cap_y"]]
    )

    result = resolve_capability_convergence_offline(
        (_node("cap_new", "X again"),),
        incoming_embeddings={"cap_new": (1.0, 0.0)},
        single_tenant_graph=graph,
        threshold=_THRESHOLD,
    )

    assert [(r.canonical_id, r.match_kind) for r in result.resolutions] == [("cap_new", "new")]
    assert result.near_misses == ()


def test_offline_tombstone_without_embedding_is_not_counted_as_skipped(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    graph = _FakeGraph(
        capability_rows=[["cap_x", "X", None], ["cap_y", "Y", [0.0, 1.0]]],
        merged_rows=[["cap_x", "cap_y"]],
    )

    resolve_capability_convergence_offline(
        (_node("cap_new", "Fresh"),),
        incoming_embeddings={"cap_new": (0.5, 0.5)},
        single_tenant_graph=graph,
        threshold=_THRESHOLD,
        emitter=emitter,
    )
    emitter.flush()

    warnings = [
        row
        for row in read_lines(log_path)
        if row.get("action") == "resolve_capability_convergence_offline"
    ]
    assert warnings == []


@pytest.mark.parametrize(
    "merged_rows",
    [
        pytest.param([["cap_a", "cap_b"], ["cap_b", "cap_a"]], id="cycle"),
        pytest.param([["cap_a", "cap_gone"]], id="terminal-missing"),
        pytest.param([["cap_a", None]], id="merged-without-edge"),
    ],
)
def test_offline_broken_redirect_raises_before_any_write(merged_rows: list[object]) -> None:
    graph = _FakeGraph(
        capability_rows=[["cap_a", "A", None], ["cap_b", "B", None]], merged_rows=merged_rows
    )

    with pytest.raises(CompanyMergeValidationError):
        resolve_capability_convergence_offline(
            (_node("cap_a", "A"),),
            incoming_embeddings={},
            single_tenant_graph=graph,
            threshold=_THRESHOLD,
        )
