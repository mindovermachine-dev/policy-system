"""Behaviour of `restore.snapshot_merge_writer`, Restore's interim offline writer (issue #210, D1).

The module is a verbatim copy of the pre-#210 `company_merge.graph_writer` write
functions, used against the staged `GRAPH.COPY` snapshot until #214 captures the
offline merge as gateway primitives. These tests pin the Cypher it issues (the
snapshot is a raw FalkorDB key, so the statement text IS the behaviour) so the
copy cannot drift while Company Merge's own writers are rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest

from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.models import (
    BareEdge,
    BaselineGraph,
    BaselineNode,
    CanonicalResolution,
    ProvenanceEdge,
)
from ps_service.restore import snapshot_merge_writer as writer

_OWNER = ("alice@example.com", "https://idp.example/")
_CREATE_ONLY = "MERGE (n:{label} {{id: $id}}) ON CREATE SET n += $properties"


@dataclass
class _Call:
    query: str
    params: dict[str, object] | None


class _Result:
    def __init__(self, rows: list[list[object]]) -> None:
        self._rows = rows

    @property
    def result_set(self) -> list[object]:
        return list(self._rows)


class _RecordingGraph:
    """FalkorDB boundary double: records every statement, answers reads from `rows`."""

    def __init__(self, rows: list[list[object]] | None = None) -> None:
        self.calls: list[_Call] = []
        self._rows = rows or []

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        self.calls.append(_Call(q, params))
        return _Result(self._rows)


def _node(node_id: str, **properties: str | float) -> BaselineNode:
    return BaselineNode(id=node_id, properties=dict(properties))


def test_role_requirement_and_provenance_statements_keep_their_shape_and_order() -> None:
    graph = _RecordingGraph()

    writer.persist_role_and_requirement_passthrough(
        graph,
        "CRA-1",
        {"title": "Cyber Resilience Act"},
        (_node("role_a", name="Manufacturer"),),
        (_node("req_a", text="Secure by design"),),
        (
            ProvenanceEdge("DEFINES", "role_a", "art 3"),
            ProvenanceEdge("EXPRESSES", "req_a", "art 13"),
        ),
    )

    assert [(call.query, call.params) for call in graph.calls] == [
        (
            "MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties",
            {"id": "CRA-1", "properties": {"title": "Cyber Resilience Act"}},
        ),
        (
            _CREATE_ONLY.format(label="Role"),
            {"id": "role_a", "properties": {"name": "Manufacturer"}},
        ),
        (
            _CREATE_ONLY.format(label="Requirement"),
            {"id": "req_a", "properties": {"text": "Secure by design"}},
        ),
        (
            (
                "MATCH (r:RegulatoryInstrument {id: $regulatory_instrument_id}), "
                "(n:Role {id: $target_id}) MERGE (r)-[e:DEFINES]->(n) "
                "SET e.source_ref = $source_ref"
            ),
            {
                "regulatory_instrument_id": "CRA-1",
                "target_id": "role_a",
                "source_ref": "art 3",
            },
        ),
        (
            (
                "MATCH (r:RegulatoryInstrument {id: $regulatory_instrument_id}), "
                "(n:Requirement {id: $target_id}) MERGE (r)-[e:EXPRESSES]->(n) "
                "SET e.source_ref = $source_ref"
            ),
            {
                "regulatory_instrument_id": "CRA-1",
                "target_id": "req_a",
                "source_ref": "art 13",
            },
        ),
    ]


def test_obligations_practice_areas_and_risk_paths_are_created_only() -> None:
    graph = _RecordingGraph()

    writer.persist_obligation_passthrough(graph, (_node("obl_a", text="x"),))
    writer.persist_practice_area_and_risk_path_passthrough(
        graph, (_node("pa_a", name="Privacy"),), (_node("rp_a", name="Breach"),)
    )

    assert [call.query for call in graph.calls] == [
        _CREATE_ONLY.format(label="Obligation"),
        _CREATE_ONLY.format(label="PracticeArea"),
        _CREATE_ONLY.format(label="RiskPath"),
    ]
    assert [cast("dict[str, object]", call.params)["id"] for call in graph.calls] == [
        "obl_a",
        "pa_a",
        "rp_a",
    ]


def test_only_new_resolutions_are_minted_with_created_at_and_embedding() -> None:
    graph = _RecordingGraph()
    resolutions = (
        CanonicalResolution(
            incoming_id="cap_new",
            canonical_id="cap_new",
            match_kind="new",
            embedding=(0.5, 0.25),
        ),
        CanonicalResolution(
            incoming_id="cap_old", canonical_id="cap_existing", match_kind="exact", embedding=None
        ),
    )

    writer.persist_canonical_nodes(
        graph,
        (_node("cap_new", name="New"), _node("cap_old", name="Old")),
        resolutions,
        kind="Capability",
    )

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert call.query == _CREATE_ONLY.format(label="Capability")
    params = cast("dict[str, object]", call.params)
    properties = cast("dict[str, object]", params["properties"])
    assert params["id"] == "cap_new"
    assert properties["name"] == "New"
    assert properties["embedding"] == [0.5, 0.25]
    assert isinstance(properties["created_at"], str)


def test_rewired_edges_are_rewritten_through_the_canonical_mapping() -> None:
    graph = _RecordingGraph()

    writer.persist_rewired_edges(
        graph,
        (BareEdge("REQUIRES", "obl_a", "cap_incoming"),),
        {"cap_incoming": "cap_canonical"},
    )

    assert [(call.query, call.params) for call in graph.calls] == [
        (
            (
                "MATCH (s:Obligation {id: $source_id}), (t:Capability {id: $target_id}) "
                "MERGE (s)-[:REQUIRES]->(t)"
            ),
            {"source_id": "obl_a", "target_id": "cap_canonical"},
        )
    ]


def test_a_requires_edge_without_a_mapping_entry_raises_before_any_statement() -> None:
    graph = _RecordingGraph()

    with pytest.raises(CompanyMergePersistenceError, match="no CanonicalResolution entry"):
        writer.persist_rewired_edges(graph, (BareEdge("REQUIRES", "obl_a", "cap_x"),), {})

    assert graph.calls == []


def test_embedding_backfill_sets_the_embedding_only_where_it_is_null() -> None:
    graph = _RecordingGraph()

    writer.backfill_canonical_embeddings(graph, kind="Capability", embeddings={"cap_a": (0.1, 0.2)})

    assert [(call.query, call.params) for call in graph.calls] == [
        (
            "MATCH (n:Capability {id: $id}) WHERE n.embedding IS NULL SET n.embedding = $embedding",
            {"id": "cap_a", "embedding": [0.1, 0.2]},
        )
    ]


def test_governance_drafts_are_created_only_and_forced_to_draft() -> None:
    graph = _RecordingGraph()

    counts = writer.persist_governance_drafts(
        graph,
        (_node("pol_a", title="P", status="approved"),),
        (_node("std_a", title="S"),),
        (_node("ctrl_a", title="C"),),
        owner=_OWNER,
        policy_embeddings={"pol_a": (0.5,)},
    )

    assert [call.query for call in graph.calls] == [
        _CREATE_ONLY.format(label="Policy"),
        _CREATE_ONLY.format(label="Standard"),
        _CREATE_ONLY.format(label="Control"),
    ]
    policy = cast(
        "dict[str, object]", cast("dict[str, object]", graph.calls[0].params)["properties"]
    )
    assert policy["status"] == "draft"
    assert policy["version"] == "1"
    assert (policy["owner_subject"], policy["owner_issuer"]) == _OWNER
    assert policy["embedding"] == [0.5]
    assert isinstance(policy["created_at"], str)
    assert (counts.policies, counts.standards, counts.controls) == (1, 1, 1)
    assert counts.status_overridden == 1


def test_classification_endpoint_validation_reads_once_and_names_missing_endpoints() -> None:
    graph = _RecordingGraph(rows=[["pa_a"]])

    with pytest.raises(CompanyMergePersistenceError, match=r"\['cap_x'\]"):
        writer.validate_classification_edge_endpoints(
            graph, (BareEdge("COVERS", "pa_a", "cap_x"),), {}
        )

    assert [call.query for call in graph.calls] == [
        "UNWIND $ids AS id MATCH (n {id: id}) RETURN id"
    ]


def test_classification_write_counts_has_the_six_audit_keys() -> None:
    baseline = BaselineGraph(
        regulatory_instrument_id="CRA-1",
        regulatory_instrument_properties={},
        role_nodes=(),
        requirement_nodes=(),
        obligation_nodes=(),
        capability_nodes=(),
        provenance_edges=(),
        bare_edges=(),
        practice_area_nodes=(_node("pa_a"),),
        risk_path_nodes=(_node("rp_a"), _node("rp_b")),
        classification_edges=(
            BareEdge("COVERS", "pa_a", "cap_a"),
            BareEdge("VERIFIED_BY", "rp_a", "ctrl_a"),
        ),
    )

    assert writer.classification_write_counts(baseline) == {
        "practice_area_count": 1,
        "risk_path_count": 2,
        "covers_count": 1,
        "owns_count": 0,
        "mitigated_by_count": 0,
        "verified_by_count": 1,
    }
