"""Tests for `ps_service.company_merge.graph_writer.persist_canonical_nodes`
(PLAN_REVIEWED.md §10 Increment 11, mint half): the `ON CREATE SET` canonical
Capability node writer (Capability is the only canonically-deduped kind since
#42) -- the load-bearing invariant that makes "existing canonical node's
properties are never overwritten" a database-engine guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from ps_service.company_merge.graph_writer import persist_canonical_nodes
from ps_service.company_merge.models import BaselineNode, CanonicalResolution


def _pop_created_at(params: dict[str, object] | None) -> str:
    """Pop and validate the non-deterministic `created_at` value out of a
    mint call's `$properties` dict, so the remaining dict can still be
    asserted via exact equality (Issue #35, Slice 1 -- PLAN.md §2.2/Slice 1's
    "Existing-test impact" note).
    """
    assert params is not None
    properties = cast("dict[str, object]", params["properties"])
    created_at = properties.pop("created_at")
    assert isinstance(created_at, str)
    datetime.fromisoformat(created_at)  # round-trips without raising
    return created_at


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


def _canonical_node(
    node_id: str = "capability_abc123", text: str = "Do the thing."
) -> BaselineNode:
    return BaselineNode(id=node_id, properties={"text": text, "confidence": 0.9})


def test_new_resolution_mints_node_with_on_create_set_and_embedding() -> None:
    graph = _FakeGraph()
    node = _canonical_node()
    embedding = (0.1, 0.2, 0.3)
    resolution = CanonicalResolution(
        incoming_id=node.id, canonical_id=node.id, match_kind="new", embedding=embedding
    )

    persist_canonical_nodes(graph, (node,), (resolution,), kind="Capability")

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert call.query == "MERGE (n:Capability {id: $id}) ON CREATE SET n += $properties"
    assert "ON CREATE SET" in call.query
    _pop_created_at(call.params)
    assert call.params == {
        "id": node.id,
        "properties": {"text": "Do the thing.", "confidence": 0.9, "embedding": [0.1, 0.2, 0.3]},
    }


def test_new_resolution_with_no_embedding_gets_no_embedding_key() -> None:
    """A mint that never triggered any comparison (embedding=None) gets no
    `embedding` key at all in its properties -- not `None`, not an empty
    list.
    """
    graph = _FakeGraph()
    node = _canonical_node()
    resolution = CanonicalResolution(
        incoming_id=node.id, canonical_id=node.id, match_kind="new", embedding=None
    )

    persist_canonical_nodes(graph, (node,), (resolution,), kind="Capability")

    assert len(graph.calls) == 1
    call = graph.calls[0]
    _pop_created_at(call.params)
    # Exact dict equality already proves no "embedding" key is present --
    # a naive "embedding" in properties check would need an unsound cast
    # against call.params's own `dict[str, object]` typing.
    assert call.params == {
        "id": node.id,
        "properties": {"text": "Do the thing.", "confidence": 0.9},
    }


def test_exact_match_resolution_gets_no_write_call() -> None:
    graph = _FakeGraph()
    node = _canonical_node()
    resolution = CanonicalResolution(
        incoming_id=node.id, canonical_id=node.id, match_kind="exact", embedding=None
    )

    persist_canonical_nodes(graph, (node,), (resolution,), kind="Capability")

    assert graph.calls == []


def test_semantic_match_resolution_gets_no_write_call() -> None:
    graph = _FakeGraph()
    node = _canonical_node(node_id="capability_incoming", text="Some duty.")
    resolution = CanonicalResolution(
        incoming_id=node.id,
        canonical_id="capability_existing",
        match_kind="semantic",
        embedding=None,
    )

    persist_canonical_nodes(graph, (node,), (resolution,), kind="Capability")

    assert graph.calls == []


def test_mixed_resolutions_only_write_for_new() -> None:
    graph = _FakeGraph()
    exact_node = _canonical_node(node_id="capability_exact", text="Existing duty.")
    new_node = _canonical_node(node_id="capability_new", text="New duty.")
    semantic_node = _canonical_node(node_id="capability_semantic", text="Matched duty.")

    resolutions = (
        CanonicalResolution(
            incoming_id=exact_node.id,
            canonical_id=exact_node.id,
            match_kind="exact",
            embedding=None,
        ),
        CanonicalResolution(
            incoming_id=new_node.id,
            canonical_id=new_node.id,
            match_kind="new",
            embedding=(0.5, 0.6),
        ),
        CanonicalResolution(
            incoming_id=semantic_node.id,
            canonical_id="capability_existing_other",
            match_kind="semantic",
            embedding=None,
        ),
    )

    persist_canonical_nodes(
        graph, (exact_node, new_node, semantic_node), resolutions, kind="Capability"
    )

    assert len(graph.calls) == 1
    _pop_created_at(graph.calls[0].params)
    assert graph.calls[0].params == {
        "id": new_node.id,
        "properties": {"text": "New duty.", "confidence": 0.9, "embedding": [0.5, 0.6]},
    }


def test_persist_canonical_nodes_mint_sets_created_at() -> None:
    """Issue #35, Slice 1 (PLAN.md §2.2): a `match_kind="new"` mint's
    `$properties` dict always carries a `created_at` key -- an ISO-8601 UTC
    timestamp that round-trips through `datetime.fromisoformat` -- needed
    later for AC-BI-006's deterministic winner-selection query; this slice
    only adds the write.
    """
    graph = _FakeGraph()
    node = _canonical_node()
    resolution = CanonicalResolution(
        incoming_id=node.id, canonical_id=node.id, match_kind="new", embedding=None
    )

    persist_canonical_nodes(graph, (node,), (resolution,), kind="Capability")

    assert len(graph.calls) == 1
    params = graph.calls[0].params
    assert params is not None
    properties = cast("dict[str, object]", params["properties"])
    assert "created_at" in properties
    created_at = properties["created_at"]
    assert isinstance(created_at, str)
    datetime.fromisoformat(created_at)


def test_capability_kind_writes_capability_label() -> None:
    graph = _FakeGraph()
    node = BaselineNode(
        id="capability_abc123", properties={"name": "Encrypt data", "confidence": 0.8}
    )
    resolution = CanonicalResolution(
        incoming_id=node.id, canonical_id=node.id, match_kind="new", embedding=None
    )

    persist_canonical_nodes(graph, (node,), (resolution,), kind="Capability")

    assert graph.calls[0].query == "MERGE (n:Capability {id: $id}) ON CREATE SET n += $properties"
