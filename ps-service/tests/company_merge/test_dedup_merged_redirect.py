"""Live exact-key redirect over `merged` Capability tombstones (issue #190, AC-BI-008/009).

`dedupe_canonical_nodes` follows `MERGED_INTO` for an incoming Capability id that
equals a tombstone id, drops tombstones from the semantic-match pool, and fails
closed (no write) on a broken redirect chain.
"""

from __future__ import annotations

import pytest

from ps_service.company_merge.dedup import dedupe_canonical_nodes
from ps_service.company_merge.errors import CompanyMergeValidationError
from ps_service.company_merge.models import BaselineNode, DedupResult

_MODEL = "fake-embed-model"
_THRESHOLD = 0.85


class _FakeQueryResult:
    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeGraph:
    """Satisfies `GraphHandle`; records every query so tests can count reads and writes."""

    def __init__(
        self,
        *,
        capability_rows: list[object],
        merged_rows: list[object],
        policy_rows: list[object] | None = None,
    ) -> None:
        self._capability_rows = capability_rows
        self._merged_rows = merged_rows
        self._policy_rows = policy_rows or []
        self.queries: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.queries.append(q)
        if "MERGED_INTO" in q:
            return _FakeQueryResult(self._merged_rows)
        if "(n:Capability) RETURN" in q:
            return _FakeQueryResult(self._capability_rows)
        if "(n:Policy) RETURN" in q:
            return _FakeQueryResult(self._policy_rows)
        raise AssertionError(f"unexpected query issued: {q!r}")


def _incoming(node_id: str, name: str, embedding: tuple[float, ...] | None = None) -> BaselineNode:
    return BaselineNode(
        id=node_id, properties={"name": name, "confidence": 0.9}, embedding=embedding
    )


def _dedupe(graph: _FakeGraph, *nodes: BaselineNode) -> DedupResult:
    return dedupe_canonical_nodes(
        tuple(nodes),
        kind="Capability",
        single_tenant_graph=graph,
        model=_MODEL,
        threshold=_THRESHOLD,
    )


def test_incoming_tombstone_id_resolves_to_survivor_with_no_mint() -> None:
    graph = _FakeGraph(
        capability_rows=[["cap_x", "X", None], ["cap_y", "Y", None]],
        merged_rows=[["cap_x", "cap_y"]],
    )

    result = _dedupe(graph, _incoming("cap_x", "X"))

    assert [(r.incoming_id, r.canonical_id, r.match_kind) for r in result.resolutions] == [
        ("cap_x", "cap_y", "redirected")
    ]
    assert result.near_misses == ()


def test_tombstone_is_never_a_semantic_match_candidate() -> None:
    vector = (1.0, 0.0)
    graph = _FakeGraph(
        capability_rows=[["cap_x", "X", list(vector)]],
        merged_rows=[["cap_x", "cap_y"]],
    )

    # Incoming embedding is identical to the tombstone's. Were the tombstone in
    # the pool it would be a semantic merge target (or an embedding call would be
    # needed); with an empty pool the node simply mints.
    result = _dedupe(graph, _incoming("cap_new", "X again", vector))

    assert [(r.canonical_id, r.match_kind) for r in result.resolutions] == [("cap_new", "new")]
    assert result.near_misses == ()


def test_chain_is_followed_to_terminal_survivor() -> None:
    graph = _FakeGraph(
        capability_rows=[["cap_a", "A", None], ["cap_b", "B", None], ["cap_c", "C", None]],
        merged_rows=[["cap_a", "cap_b"], ["cap_b", "cap_c"]],
    )

    result = _dedupe(graph, _incoming("cap_a", "A"))

    assert result.resolutions[0].canonical_id == "cap_c"
    assert result.resolutions[0].match_kind == "redirected"


@pytest.mark.parametrize(
    ("capability_rows", "merged_rows"),
    [
        pytest.param(
            [["cap_a", "A", None], ["cap_b", "B", None]],
            [["cap_a", "cap_b"], ["cap_b", "cap_a"]],
            id="cycle",
        ),
        pytest.param([["cap_a", "A", None]], [["cap_a", "cap_gone"]], id="terminal-missing"),
        pytest.param([["cap_a", "A", None]], [["cap_a", None]], id="merged-without-edge"),
        pytest.param(
            [["cap_a", "A", None], ["cap_b", "B", None]],
            [["cap_a", "cap_b"], ["cap_b", None]],
            id="chain-ends-in-edgeless-tombstone",
        ),
    ],
)
def test_broken_redirect_raises_before_any_write(
    capability_rows: list[object], merged_rows: list[object]
) -> None:
    graph = _FakeGraph(capability_rows=capability_rows, merged_rows=merged_rows)

    with pytest.raises(CompanyMergeValidationError):
        _dedupe(graph, _incoming("cap_a", "A"))

    assert all("MERGE" not in q.replace("MERGED_INTO", "") for q in graph.queries)


def test_policy_kind_issues_no_redirect_read() -> None:
    graph = _FakeGraph(capability_rows=[], merged_rows=[], policy_rows=[])

    dedupe_canonical_nodes(
        (BaselineNode(id="pol_1", properties={"title": "P", "confidence": 0.9}),),
        kind="Policy",
        single_tenant_graph=graph,
        model=_MODEL,
        threshold=_THRESHOLD,
    )

    assert len(graph.queries) == 1
    assert all("MERGED_INTO" not in q for q in graph.queries)
