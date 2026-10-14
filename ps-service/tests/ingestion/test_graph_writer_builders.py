"""The pure builders of the external (Cellar/ELI) native write.

Nothing here submits a group to the pipeline: each builder turns adapter output into gateway
primitives, and the one test that submits uses the gateway rig to prove the group the builders
assemble is one valid, ordered, committable group.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING, cast

import pytest

from ps_service.graph_gateway.models import MutationGroup, NodeRef, UpsertEdge, UpsertNode
from ps_service.ingestion.errors import IngestionPersistenceError
from ps_service.ingestion.graph_writer import (
    NativeWrites,
    build_native_group,
    build_native_writes,
    read_existing_nodes,
)
from ps_service.ingestion.models import (
    RegulatoryInstrumentMetadata,
    StructuralEdge,
    StructuralNode,
)

if TYPE_CHECKING:
    from graph_gateway._fakes import GatewayRig

_GRAPH = "cra_native"
_RI_ID = "CRA-1.0"


def _metadata(
    *, instrument_type: str | None = "regulation", celex: str | None = None
) -> RegulatoryInstrumentMetadata:
    """External metadata; with no `instrument_type` the instrument is an internal one."""
    return RegulatoryInstrumentMetadata(
        title="Cyber Resilience Act",
        jurisdiction="EU",
        effective_date=date(2027, 12, 11),
        version="1.0",
        status="active",
        source_type="external" if instrument_type is not None else "internal",
        instrument_type=instrument_type,  # pyright: ignore[reportArgumentType] -- the literal union is checked by the model
        celex=celex,
    )


def _node(element_type: str, node_id: str, **extra: str | int) -> StructuralNode:
    return StructuralNode(
        element_type, node_id, {"text": "text", "citation_ref": "Art. 1", "order": 1, **extra}
    )


def _edge(parent_type: str, parent_id: str, child_type: str, child_id: str) -> StructuralEdge:
    return StructuralEdge(parent_type, parent_id, child_type, child_id)


class _Rows:
    def __init__(self, rows: list[list[object]]) -> None:
        self.result_set: list[object] = list(rows)


class _ReadGraph:
    """A read handle holding the node ids already in the graph, recording each read it serves."""

    def __init__(self, existing: dict[str, set[str]]) -> None:
        self._existing = existing
        self.reads: list[tuple[str, dict[str, object] | None]] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _Rows:
        self.reads.append((q, params))
        assert q.lstrip().startswith("MATCH"), q
        label = q.split("(n:", 1)[1].split(")", 1)[0].split(" ", 1)[0]
        ids = cast("list[str]", params["ids"] if params else [])
        return _Rows([[i] for i in ids if i in self._existing.get(label, set())])


def _writes(
    nodes: tuple[StructuralNode, ...] = (),
    edges: tuple[StructuralEdge, ...] = (),
    metadata: RegulatoryInstrumentMetadata | None = None,
) -> NativeWrites:
    return build_native_writes(_RI_ID, metadata or _metadata(), nodes, edges)


# --- the RegulatoryInstrument node ------------------------------------------------------


def test_instrument_node_carries_the_metadata_with_an_iso_effective_date() -> None:
    writes = _writes(metadata=_metadata(celex="32024R2847"))

    assert writes.instrument == UpsertNode(
        label="RegulatoryInstrument",
        id=_RI_ID,
        properties={
            "title": "Cyber Resilience Act",
            "jurisdiction": "EU",
            "effective_date": "2027-12-11",
            "version": "1.0",
            "status": "active",
            "source_type": "external",
            "instrument_type": "regulation",
            "celex": "32024R2847",
        },
    )


def test_instrument_node_omits_instrument_type_and_celex_when_absent() -> None:
    writes = _writes(metadata=_metadata(instrument_type=None))

    assert "instrument_type" not in writes.instrument.properties
    assert "celex" not in writes.instrument.properties


# --- structural nodes -------------------------------------------------------------------


def test_structural_nodes_keep_input_order_labels_and_properties() -> None:
    nodes = (_node("ARTICLE", "CRA#art_1"), _node("PARAGRAPH", "CRA#art_1.001"))

    writes = _writes(nodes)

    assert writes.nodes == (
        UpsertNode(label="ARTICLE", id="CRA#art_1", properties=dict(nodes[0].properties)),
        UpsertNode(label="PARAGRAPH", id="CRA#art_1.001", properties=dict(nodes[1].properties)),
    )


def test_an_unknown_node_element_type_raises_before_anything_is_built() -> None:
    nodes = (_node("ARTICLE", "CRA#art_1"), _node("BOGUS", "CRA#bogus_1"))

    with pytest.raises(IngestionPersistenceError, match="element_type"):
        _writes(nodes)


def test_a_node_property_the_gateway_reserves_raises_instead_of_rewriting_the_node_id() -> None:
    nodes = (StructuralNode("ARTICLE", "CRA#art_1", {"id": "someone-else"}),)

    with pytest.raises(IngestionPersistenceError, match="CRA#art_1"):
        _writes(nodes)


# --- HAS edges ---------------------------------------------------------------------------


def test_has_edges_have_the_identity_of_type_parent_and_child() -> None:
    nodes = (_node("ARTICLE", "CRA#art_1"), _node("PARAGRAPH", "CRA#art_1.001"))
    edges = (_edge("ARTICLE", "CRA#art_1", "PARAGRAPH", "CRA#art_1.001"),)

    writes = _writes(nodes, edges)

    assert writes.edges == (
        UpsertEdge(
            type="HAS",
            identity="HAS:CRA#art_1->CRA#art_1.001",
            source=NodeRef(label="ARTICLE", id="CRA#art_1"),
            target=NodeRef(label="PARAGRAPH", id="CRA#art_1.001"),
        ),
    )


def test_the_instrument_placeholder_parent_id_is_replaced_by_the_real_instrument_id() -> None:
    """The adapter stamps the CELEX as the parent of every top-level edge."""
    nodes = (_node("ANNEX", "32024R2847#anx_I"),)
    edges = (_edge("RegulatoryInstrument", "32024R2847", "ANNEX", "32024R2847#anx_I"),)

    writes = _writes(nodes, edges)

    assert writes.edges[0].source == NodeRef(label="RegulatoryInstrument", id=_RI_ID)
    assert writes.edges[0].identity == "HAS:CRA-1.0->32024R2847#anx_I"


def test_an_unknown_edge_element_type_raises_even_after_valid_nodes_and_edges() -> None:
    nodes = (_node("ARTICLE", "CRA#art_1"),)
    edges = (
        _edge("RegulatoryInstrument", _RI_ID, "ARTICLE", "CRA#art_1"),
        _edge("ARTICLE", "CRA#art_1", "BOGUS", "CRA#bogus_1"),
    )

    with pytest.raises(IngestionPersistenceError, match="child_element_type"):
        _writes(nodes, edges)


def test_an_unknown_parent_element_type_raises() -> None:
    edges = (_edge("BOGUS", "CRA#bogus_1", "ARTICLE", "CRA#art_1"),)

    with pytest.raises(IngestionPersistenceError, match="parent_element_type"):
        _writes((_node("ARTICLE", "CRA#art_1"),), edges)


# --- endpoints outside the group ---------------------------------------------


def test_nothing_is_read_when_every_edge_endpoint_is_in_the_group() -> None:
    nodes = (_node("ARTICLE", "CRA#art_1"),)
    writes = _writes(nodes, (_edge("RegulatoryInstrument", _RI_ID, "ARTICLE", "CRA#art_1"),))
    graph = _ReadGraph({})

    existing = read_existing_nodes(graph, writes)

    assert existing == frozenset()
    assert graph.reads == []


def test_endpoints_outside_the_group_are_read_once_per_label_in_one_batch() -> None:
    edges = (
        _edge("ARTICLE", "OLD#art_1", "PARAGRAPH", "CRA#p_1"),
        _edge("ARTICLE", "OLD#art_2", "PARAGRAPH", "CRA#p_2"),
    )
    nodes = (_node("PARAGRAPH", "CRA#p_1"), _node("PARAGRAPH", "CRA#p_2"))
    writes = _writes(nodes, edges)
    graph = _ReadGraph({"ARTICLE": {"OLD#art_1"}})

    existing = read_existing_nodes(graph, writes)

    assert existing == frozenset({("ARTICLE", "OLD#art_1")})
    assert len(graph.reads) == 1
    query, params = graph.reads[0]
    assert params is not None
    assert sorted(params["ids"]) == ["OLD#art_1", "OLD#art_2"]  # pyright: ignore[reportArgumentType]
    assert "OLD#art_1" not in query


def test_an_edge_whose_parent_is_nowhere_is_dropped_without_error_and_the_rest_are_kept() -> None:
    nodes = (_node("ARTICLE", "CRA#art_1"), _node("PARAGRAPH", "CRA#p_1"))
    edges = (
        _edge("RegulatoryInstrument", _RI_ID, "ARTICLE", "CRA#art_1"),
        _edge("ARTICLE", "GHOST#art", "PARAGRAPH", "CRA#p_1"),
    )
    writes = _writes(nodes, edges)

    group = build_native_group(
        writes,
        existing=frozenset(),
        graph_name=_GRAPH,
        audit_event_id=str(uuid.uuid4()),
    )

    edge_identities = [p.identity for p in group.primitives if isinstance(p, UpsertEdge)]
    assert edge_identities == ["HAS:CRA-1.0->CRA#art_1"]


def test_an_edge_to_a_node_of_a_sibling_version_already_in_the_graph_is_kept() -> None:
    nodes = (_node("PARAGRAPH", "CRA#p_1"),)
    edges = (_edge("ARTICLE", "OLD#art_1", "PARAGRAPH", "CRA#p_1"),)
    writes = _writes(nodes, edges)

    group = build_native_group(
        writes,
        existing=frozenset({("ARTICLE", "OLD#art_1")}),
        graph_name=_GRAPH,
        audit_event_id=str(uuid.uuid4()),
    )

    assert [p.identity for p in group.primitives if isinstance(p, UpsertEdge)] == [
        "HAS:OLD#art_1->CRA#p_1"
    ]


# --- the group ---------------------------------------------------------------------------


def test_the_group_is_the_instrument_then_the_nodes_then_the_edges_on_the_native_graph() -> None:
    nodes = (_node("ARTICLE", "CRA#art_1"), _node("PARAGRAPH", "CRA#p_1"))
    edges = (
        _edge("RegulatoryInstrument", "32024R2847", "ARTICLE", "CRA#art_1"),
        _edge("ARTICLE", "CRA#art_1", "PARAGRAPH", "CRA#p_1"),
    )
    audit_event_id = str(uuid.uuid4())

    group = build_native_group(
        _writes(nodes, edges),
        existing=frozenset(),
        graph_name=_GRAPH,
        audit_event_id=audit_event_id,
    )

    assert isinstance(group, MutationGroup)
    assert group.graph == _GRAPH
    assert group.audit_event_id == audit_event_id
    assert group.checkpoint_requested is False
    assert [type(p).__name__ for p in group.primitives] == [
        "UpsertNode",
        "UpsertNode",
        "UpsertNode",
        "UpsertEdge",
        "UpsertEdge",
    ]
    assert [p.id for p in group.primitives if isinstance(p, UpsertNode)] == [
        _RI_ID,
        "CRA#art_1",
        "CRA#p_1",
    ]


def test_the_assembled_group_commits_as_one_group_and_collapses_equal_edges(
    rig: GatewayRig,
) -> None:
    audit_event_id = str(uuid.uuid4())
    rig.store.audit_rows.add(audit_event_id)
    nodes = (_node("ARTICLE", "CRA#art_1"),)
    edges = (
        _edge("RegulatoryInstrument", "32024R2847", "ARTICLE", "CRA#art_1"),
        _edge("RegulatoryInstrument", _RI_ID, "ARTICLE", "CRA#art_1"),
    )
    group = build_native_group(
        _writes(nodes, edges),
        existing=frozenset(),
        graph_name=_GRAPH,
        audit_event_id=audit_event_id,
    )

    outcome = rig.gateway.submit_group(group)

    assert outcome.status == "applied"
    assert len(rig.store.groups) == 1
    assert rig.store.logged_count(_GRAPH) == 3
    graph = rig.graphs.open(_GRAPH)
    assert set(graph.nodes) == {
        ("RegulatoryInstrument", _RI_ID),
        ("ARTICLE", "CRA#art_1"),
    }
    assert len(graph.edges) == 1
