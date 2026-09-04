"""Test for `ps_service.company_merge.dedup.resolve_capability_convergence_offline`
(PLAN.md Slice 5.4, CHANGES.md MA1): in-batch convergence.

Mirrors `test_dedup_combined_resolution.py`'s
`test_dedupe_canonical_nodes_in_run_convergence_onto_first_incoming_node`
shape exactly, adapted to the offline function's artifact-supplied-embedding
signature: two incoming Capabilities with no existing target-graph match,
whose artifact-supplied embeddings score above threshold against EACH
OTHER -- the second must converge onto the first's freshly-minted id, not
mint a separate canonical node (MA1's working-index growth fix, mirroring
`dedupe_canonical_nodes`'s own in-run convergence mechanism,
`dedup.py:274-280`).
"""

from __future__ import annotations

from ps_service.company_merge.dedup import resolve_capability_convergence_offline
from ps_service.company_merge.models import BaselineNode

_THRESHOLD = 0.85


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _ScriptedSingleTenantGraph:
    """Satisfies `GraphHandle` structurally -- an empty target index, so
    neither incoming node has any pre-existing match.
    """

    def __init__(self, *, capability_rows: list[object]) -> None:
        self._capability_rows = capability_rows

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "(n:Capability) RETURN" in q:
            return _FakeQueryResult(self._capability_rows)
        raise AssertionError(f"unexpected query issued: {q!r}")


def _capability(node_id: str, name: str) -> BaselineNode:
    return BaselineNode(id=node_id, properties={"name": name})


def test_resolve_capability_convergence_offline_in_batch_convergence_onto_first_incoming_node() -> (
    None
):
    first_id = "incoming_capability_first_alpha"
    second_id = "incoming_capability_second_beta"
    graph = _ScriptedSingleTenantGraph(capability_rows=[])
    incoming_nodes = (
        _capability(first_id, "Data Encryption at Rest"),
        _capability(second_id, "Encrypt Data While Stored"),
    )

    result = resolve_capability_convergence_offline(
        incoming_nodes,
        incoming_embeddings={first_id: (1.0, 0.0), second_id: (1.0, 0.0)},
        single_tenant_graph=graph,
        threshold=_THRESHOLD,
    )

    assert len(result.resolutions) == 2
    first_resolution = next(r for r in result.resolutions if r.incoming_id == first_id)
    second_resolution = next(r for r in result.resolutions if r.incoming_id == second_id)
    assert first_resolution.match_kind == "new"
    assert first_resolution.canonical_id == first_id
    assert second_resolution.match_kind == "semantic"
    assert second_resolution.canonical_id == first_id
