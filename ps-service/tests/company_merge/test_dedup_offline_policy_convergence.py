"""Tests for `ps_service.company_merge.dedup.resolve_capability_convergence_offline`
with `kind="Policy"` (issue #54, S6/B6, AC-BI-021): the offline counterpart to
`test_dedup_policy_convergence.py`'s live-path Policy convergence proof (exact
match and semantic match), exercised here for the OFFLINE function a restore's
baseline merge uses instead.

Per D6/CHANGES.md MA1: `resolve_capability_convergence_offline` never calls
`route_embedding`/constructs an `EmbeddingCaller` -- every embedding it scores
with is either artifact-supplied (`incoming_embeddings`) or already cached on
an existing canonical node. Mirrors `test_dedup_offline_exact_match.py`/
`test_dedup_offline_semantic_match.py`'s own fakes and conventions exactly,
widened from Capability's `n.name` text property to Policy's `n.title`.
"""

from __future__ import annotations

from ps_service.company_merge.dedup import resolve_capability_convergence_offline
from ps_service.company_merge.models import BaselineNode
from ps_service.domain_mapper.identity import policy_id

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
    `read_existing_canonical_index`'s Policy read query (`n.id, n.title,
    n.embedding`) and records every call it receives.
    """

    def __init__(self, *, policy_rows: list[object]) -> None:
        self._policy_rows = policy_rows
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        if "(n:Policy) RETURN" in q:
            return _FakeQueryResult(self._policy_rows)
        raise AssertionError(f"unexpected query issued: {q!r}")


def _policy_node(node_id: str, title: str) -> BaselineNode:
    return BaselineNode(id=node_id, properties={"title": title, "status": "draft"})


def test_resolve_convergence_offline_kind_policy_exact_match() -> None:
    """An incoming Policy whose id already equals an existing canonical
    Policy id (both computed via `policy_id(title)`) resolves via
    `resolve_exact_match`, onto that SAME id -- no semantic comparison, no
    `incoming_embeddings` entry needed.
    """
    title = "Engineering Practices Policy"
    existing_id = policy_id(title)
    graph = _ScriptedSingleTenantGraph(policy_rows=[[existing_id, title, [1.0, 0.0]]])
    incoming_nodes = (_policy_node(existing_id, title),)

    result = resolve_capability_convergence_offline(
        incoming_nodes,
        incoming_embeddings={},
        single_tenant_graph=graph,
        threshold=_THRESHOLD,
        kind="Policy",
    )

    assert len(result.resolutions) == 1
    resolution = result.resolutions[0]
    assert resolution.match_kind == "exact"
    assert resolution.incoming_id == existing_id
    assert resolution.canonical_id == existing_id
    assert resolution.embedding is None
    assert graph.calls == ["MATCH (n:Policy) RETURN n.id, n.title, n.embedding"]


def test_kind_policy_semantic_match_using_cached_embeddings_only() -> None:
    """An incoming Policy whose title differs from an existing canonical
    Policy's title (so its computed id differs too -- no exact-key match),
    but whose artifact-supplied embedding scores >= threshold against the
    existing node's own cached embedding, resolves via
    `match_kind="semantic"` onto the EXISTING Policy's canonical id -- using
    only artifact-supplied/cached vectors, never `route_embedding`.
    """
    existing_title = "Engineering Practices Policy"
    existing_id = policy_id(existing_title)
    incoming_title = "Engineering Practice Policy"  # reworded, distinct id
    incoming_id = policy_id(incoming_title)
    assert incoming_id != existing_id

    graph = _ScriptedSingleTenantGraph(policy_rows=[[existing_id, existing_title, [1.0, 0.0]]])
    incoming_nodes = (_policy_node(incoming_id, incoming_title),)

    result = resolve_capability_convergence_offline(
        incoming_nodes,
        incoming_embeddings={incoming_id: (1.0, 0.0)},
        single_tenant_graph=graph,
        threshold=_THRESHOLD,
        kind="Policy",
    )

    assert len(result.resolutions) == 1
    resolution = result.resolutions[0]
    assert resolution.incoming_id == incoming_id
    assert resolution.match_kind == "semantic"
    assert resolution.canonical_id == existing_id
    assert resolution.canonical_id != incoming_id


def test_kind_defaults_to_capability_when_omitted() -> None:
    """Regression guard: `kind` defaults to `"Capability"` (its original,
    pre-#54 scope) when the caller omits it -- every pre-existing offline
    dedup test (`test_dedup_offline_exact_match.py` et al.) calls without
    `kind=` at all and must keep resolving against the Capability index, not
    the Policy one.
    """
    existing_id = "capability_data_encryption"
    graph = _ScriptedSingleTenantGraph(policy_rows=[])

    class _CapabilityGraph:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
            self.calls.append(q)
            if "(n:Capability) RETURN" in q:
                return _FakeQueryResult([[existing_id, "Data Encryption Capability", None]])
            raise AssertionError(f"unexpected query issued: {q!r}")

    capability_graph = _CapabilityGraph()
    incoming_nodes = (
        BaselineNode(id=existing_id, properties={"name": "Data Encryption Capability"}),
    )

    result = resolve_capability_convergence_offline(
        incoming_nodes,
        incoming_embeddings={},
        single_tenant_graph=capability_graph,
        threshold=_THRESHOLD,
    )

    assert result.resolutions[0].match_kind == "exact"
    assert capability_graph.calls == ["MATCH (n:Capability) RETURN n.id, n.name, n.embedding"]
    assert graph.calls == []  # the unused Policy-shaped fake was never touched
