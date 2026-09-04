"""Tests for `ps_service.company_merge.dedup.resolve_capability_convergence_offline`
(PLAN.md Slice 5.3, D6): exact-match-first behavior.

`resolve_capability_convergence_offline` is D6's offline counterpart to
`dedupe_canonical_nodes` -- it never accepts an `EmbeddingCaller`/
`call_embedding` collaborator at all (embeddings, if any, are
artifact-supplied via `incoming_embeddings`), so "zero calls to any
embedding-related fake" for the exact-match path is proved by construction:
these tests never populate `incoming_embeddings` for the exact-matching
node, and the resolution still succeeds -- proving the exact-match branch
returns (`continue`s) before ever consulting `incoming_embeddings` or
`cosine_similarity`.

Fakes mirror `test_dedup_combined_resolution.py`'s own conventions
(`_ScriptedSingleTenantGraph` satisfies `GraphHandle` structurally,
dispatching by query substring).
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
    """Satisfies `GraphHandle` structurally -- answers
    `read_existing_canonical_index`'s one read query and records every call
    it receives, so a test can assert no write call is ever issued.
    """

    def __init__(self, *, capability_rows: list[object]) -> None:
        self._capability_rows = capability_rows
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        if "(n:Capability) RETURN" in q:
            return _FakeQueryResult(self._capability_rows)
        raise AssertionError(f"unexpected query issued: {q!r}")


def _capability(node_id: str, name: str) -> BaselineNode:
    return BaselineNode(id=node_id, properties={"name": name})


def test_exact_match_resolves_without_any_embedding_supplied() -> None:
    """An incoming Capability whose `id` already exists in the target's
    canonical index resolves via `resolve_exact_match` -- `incoming_embeddings`
    is empty, proving the exact-match branch never needed one.
    """
    existing_id = "capability_data_encryption"
    graph = _ScriptedSingleTenantGraph(
        capability_rows=[[existing_id, "Data Encryption Capability", None]]
    )
    incoming_nodes = (_capability(existing_id, "Data Encryption Capability"),)

    result = resolve_capability_convergence_offline(
        incoming_nodes,
        incoming_embeddings={},
        single_tenant_graph=graph,
        threshold=_THRESHOLD,
    )

    assert len(result.resolutions) == 1
    resolution = result.resolutions[0]
    assert resolution.match_kind == "exact"
    assert resolution.incoming_id == existing_id
    assert resolution.canonical_id == existing_id
    assert resolution.embedding is None


def test_exact_match_issues_no_write_query() -> None:
    existing_id = "capability_incident_reporting"
    graph = _ScriptedSingleTenantGraph(
        capability_rows=[[existing_id, "Incident Reporting Capability", None]]
    )
    incoming_nodes = (_capability(existing_id, "Incident Reporting Capability"),)

    resolve_capability_convergence_offline(
        incoming_nodes,
        incoming_embeddings={},
        single_tenant_graph=graph,
        threshold=_THRESHOLD,
    )

    assert graph.calls == ["MATCH (n:Capability) RETURN n.id, n.name, n.embedding"]


def test_exact_match_produces_no_near_miss_and_no_backfill() -> None:
    existing_id = "capability_vulnerability_handling"
    graph = _ScriptedSingleTenantGraph(
        capability_rows=[[existing_id, "Vulnerability Handling Capability", None]]
    )
    incoming_nodes = (_capability(existing_id, "Vulnerability Handling Capability"),)

    result = resolve_capability_convergence_offline(
        incoming_nodes,
        incoming_embeddings={},
        single_tenant_graph=graph,
        threshold=_THRESHOLD,
    )

    assert result.near_misses == ()
    assert result.embedding_backfills == {}
