"""Tests for `ps_service.company_merge.graph_writer.persist_canonical_nodes`
(PLAN_REVIEWED.md §10 Increment 11, mint half): the `ON CREATE SET` canonical
Obligation/Capability node writer -- the load-bearing invariant that makes
"existing canonical node's properties are never overwritten" a
database-engine guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass

from ps_service.company_merge.graph_writer import persist_canonical_nodes
from ps_service.company_merge.models import BaselineNode, CanonicalResolution


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _FakeQueryResult:
    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeGraph:
    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult([[0]])


def _obligation_node(node_id: str = "obligation_abc123", text: str = "Do the thing.") -> BaselineNode:
    return BaselineNode(id=node_id, properties={"text": text, "confidence": 0.9})


def test_new_resolution_mints_node_with_on_create_set_and_embedding() -> None:
    graph = _FakeGraph()
    node = _obligation_node()
    embedding = (0.1, 0.2, 0.3)
    resolution = CanonicalResolution(
        incoming_id=node.id, canonical_id=node.id, match_kind="new", embedding=embedding
    )

    persist_canonical_nodes(graph, (node,), (resolution,), kind="Obligation")

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert call.query == "MERGE (n:Obligation {id: $id}) ON CREATE SET n += $properties"
    assert "ON CREATE SET" in call.query
    assert call.params == {
        "id": node.id,
        "properties": {"text": "Do the thing.", "confidence": 0.9, "embedding": [0.1, 0.2, 0.3]},
    }


def test_new_resolution_with_no_embedding_gets_no_embedding_key() -> None:
    """A mint that never triggered any comparison (embedding=None) gets no
    `embedding` key at all in its properties -- not `None`, not an empty
    list."""
    graph = _FakeGraph()
    node = _obligation_node()
    resolution = CanonicalResolution(
        incoming_id=node.id, canonical_id=node.id, match_kind="new", embedding=None
    )

    persist_canonical_nodes(graph, (node,), (resolution,), kind="Obligation")

    assert len(graph.calls) == 1
    call = graph.calls[0]
    # Exact dict equality already proves no "embedding" key is present --
    # a naive "embedding" in properties check would need an unsound cast
    # against call.params's own `dict[str, object]` typing.
    assert call.params == {
        "id": node.id,
        "properties": {"text": "Do the thing.", "confidence": 0.9},
    }


def test_exact_match_resolution_gets_no_write_call() -> None:
    graph = _FakeGraph()
    node = _obligation_node()
    resolution = CanonicalResolution(
        incoming_id=node.id, canonical_id=node.id, match_kind="exact", embedding=None
    )

    persist_canonical_nodes(graph, (node,), (resolution,), kind="Obligation")

    assert graph.calls == []


def test_semantic_match_resolution_gets_no_write_call() -> None:
    graph = _FakeGraph()
    node = _obligation_node(node_id="obligation_incoming", text="Some duty.")
    resolution = CanonicalResolution(
        incoming_id=node.id, canonical_id="obligation_existing", match_kind="semantic", embedding=None
    )

    persist_canonical_nodes(graph, (node,), (resolution,), kind="Obligation")

    assert graph.calls == []


def test_mixed_resolutions_only_write_for_new() -> None:
    graph = _FakeGraph()
    exact_node = _obligation_node(node_id="obligation_exact", text="Existing duty.")
    new_node = _obligation_node(node_id="obligation_new", text="New duty.")
    semantic_node = _obligation_node(node_id="obligation_semantic", text="Matched duty.")

    resolutions = (
        CanonicalResolution(
            incoming_id=exact_node.id, canonical_id=exact_node.id, match_kind="exact", embedding=None
        ),
        CanonicalResolution(
            incoming_id=new_node.id,
            canonical_id=new_node.id,
            match_kind="new",
            embedding=(0.5, 0.6),
        ),
        CanonicalResolution(
            incoming_id=semantic_node.id,
            canonical_id="obligation_existing_other",
            match_kind="semantic",
            embedding=None,
        ),
    )

    persist_canonical_nodes(
        graph, (exact_node, new_node, semantic_node), resolutions, kind="Obligation"
    )

    assert len(graph.calls) == 1
    assert graph.calls[0].params == {
        "id": new_node.id,
        "properties": {"text": "New duty.", "confidence": 0.9, "embedding": [0.5, 0.6]},
    }


def test_capability_kind_writes_capability_label() -> None:
    graph = _FakeGraph()
    node = BaselineNode(id="capability_abc123", properties={"name": "Encrypt data", "confidence": 0.8})
    resolution = CanonicalResolution(
        incoming_id=node.id, canonical_id=node.id, match_kind="new", embedding=None
    )

    persist_canonical_nodes(graph, (node,), (resolution,), kind="Capability")

    assert graph.calls[0].query == "MERGE (n:Capability {id: $id}) ON CREATE SET n += $properties"
