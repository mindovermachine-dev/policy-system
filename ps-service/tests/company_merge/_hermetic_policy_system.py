"""A hermetic stand-in for a one-off live `policy_system` merge.

A one-off live merge into the developer's own `policy_system` graph cannot be asserted on a
clean instance, so `merged_graph_run` runs the same real `merge_baseline_graph` (real FalkorDB,
real Graph Write Gateway on the scratch PS Postgres, fake one-hot embeddings so no LLM is
needed) into a uniquely named graph, over two small seeded baselines that share one Capability:
a pre-existing regulation merged first, then the new one, then the new one again. It returns the
before/after snapshots such a live merge would report.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from company_merge._live_merge_assertions import (
    _NODE_LABELS,  # pyright: ignore[reportPrivateUsage]
    _snapshot_edge_ids,  # pyright: ignore[reportPrivateUsage]
    _snapshot_edge_properties,  # pyright: ignore[reportPrivateUsage]
    _snapshot_node_properties,  # pyright: ignore[reportPrivateUsage]
)
from company_merge._seed_merge_graphs import OneHotEmbeddingCaller
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.domain_mapper.identity import capability_id, obligation_id, requirement_id, role_id
from ps_service.graph_gateway.connection import connect_from_config, select_graph
from ps_service.graph_gateway.default_gateway import build_default_graph_write_gateway
from ps_service.graph_gateway.system_audit import build_system_audit_recorder
from ps_service.logging import EmitterConfig, LogEmitter, bind_run_context

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from persistence.provisioned_postgres import Provisioned

    from ps_service.company_merge.falkordb_client import GraphHandle

EXISTING_ID = "EXIST-1.0"
NEW_ID = "NEWREG-1.0"
SHARED_CAPABILITY = "Hermetic Shared Incident Reporting Capability"
MERGE_THRESHOLD = 0.9
_REGULATIONS = (EXISTING_ID, NEW_ID)


@dataclass(frozen=True)
class MergedGraphRun:
    """What the hermetic merge produced; `graph` is the uniquely named `policy_system` stand-in."""

    graph: GraphHandle
    graph_name: str
    before: dict[str, object]
    after_pass_1: dict[str, object]
    after_pass_2: dict[str, object]
    run_ids: list[str]
    log_path: Path
    shared_capability_id: str


def _snapshot(graph: GraphHandle) -> dict[str, object]:
    node_properties = _snapshot_node_properties(graph)
    return {
        "node_ids": {label: sorted(node_properties[label]) for label in _NODE_LABELS},
        "node_counts": {label: len(node_properties[label]) for label in _NODE_LABELS},
        "node_properties": node_properties,
        "edge_properties": _snapshot_edge_properties(graph),
        "edge_ids": _snapshot_edge_ids(graph),
    }


def _seed_baseline(graph: GraphHandle, instrument_id: str, tag: str) -> None:
    """One Role/Requirement/Obligation chain of its own plus one Capability of its own, and an
    Obligation of the shared Capability (identical name => identical id across regulations).
    """
    role = role_id(f"Hermetic {tag} Role", instrument_id)
    requirement = requirement_id(instrument_id, tag, "1", None)
    obligation = obligation_id(role, f"Hermetic {tag} duty text")
    own_capability = capability_id(f"Hermetic {tag} Own Capability")
    shared = capability_id(SHARED_CAPABILITY)
    obligation_2 = obligation_id(role, f"Hermetic {tag} second duty text")
    statements: list[tuple[str, dict[str, object]]] = [
        ("MERGE (n:RegulatoryInstrument {id: $id}) SET n.title = $id", {"id": instrument_id}),
        (
            "MERGE (n:Role {id: $id}) SET n.name = $v, n.confidence = 1.0",
            {"id": role, "v": f"Hermetic {tag} Role"},
        ),
        (
            (
                "MERGE (n:Requirement {id: $id}) SET n.text = $v, n.type = 'requirement', "
                "n.confidence = 1.0, n.role_id = $role"
            ),
            {"id": requirement, "v": f"Hermetic {tag} requirement", "role": role},
        ),
        (
            "MERGE (n:Obligation {id: $id}) SET n.text = $v, n.confidence = 1.0",
            {"id": obligation, "v": f"Hermetic {tag} duty text"},
        ),
        (
            "MERGE (n:Obligation {id: $id}) SET n.text = $v, n.confidence = 1.0",
            {"id": obligation_2, "v": f"Hermetic {tag} second duty text"},
        ),
        (
            "MERGE (n:Capability {id: $id}) SET n.name = $v, n.confidence = 1.0",
            {"id": own_capability, "v": f"Hermetic {tag} Own Capability"},
        ),
        (
            "MERGE (n:Capability {id: $id}) SET n.name = $v, n.confidence = 1.0",
            {"id": shared, "v": SHARED_CAPABILITY},
        ),
        (
            (
                "MATCH (r:RegulatoryInstrument {id: $a}), (n:Role {id: $b}) "
                "MERGE (r)-[e:DEFINES]->(n) SET e.source_ref = 'hermetic'"
            ),
            {"a": instrument_id, "b": role},
        ),
        (
            (
                "MATCH (r:RegulatoryInstrument {id: $a}), (n:Requirement {id: $b}) "
                "MERGE (r)-[e:EXPRESSES]->(n) SET e.source_ref = 'hermetic'"
            ),
            {"a": instrument_id, "b": requirement},
        ),
        (
            "MATCH (s:Role {id: $a}), (t:Obligation {id: $b}) MERGE (s)-[:HAS]->(t)",
            {"a": role, "b": obligation},
        ),
        (
            "MATCH (s:Role {id: $a}), (t:Obligation {id: $b}) MERGE (s)-[:HAS]->(t)",
            {"a": role, "b": obligation_2},
        ),
        (
            (
                "MATCH (s:Requirement {id: $a}), (t:Obligation {id: $b}) "
                "MERGE (s)-[:SATISFIED_BY]->(t)"
            ),
            {"a": requirement, "b": obligation},
        ),
        (
            "MATCH (s:Obligation {id: $a}), (t:Capability {id: $b}) MERGE (s)-[:REQUIRES]->(t)",
            {"a": obligation, "b": own_capability},
        ),
        (
            "MATCH (s:Obligation {id: $a}), (t:Capability {id: $b}) MERGE (s)-[:REQUIRES]->(t)",
            {"a": obligation_2, "b": shared},
        ),
    ]
    for query, params in statements:
        graph.query(query, params=params)


@pytest.fixture(scope="module")
def merged_graph_run(
    provisioned_graph_log: Provisioned, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[MergedGraphRun]:
    config = provisioned_graph_log.state_config()
    db = connect_from_config(config)
    suffix = uuid.uuid4().hex[:10]
    graph_name = f"ll11_policy_system_{suffix}"
    baseline_names = {
        EXISTING_ID: f"ll11_exist_{suffix}_baseline",
        NEW_ID: f"ll11_newreg_{suffix}_baseline",
    }
    graph = select_graph(db, graph_name)
    log_path = tmp_path_factory.mktemp("ll11_merge") / "merge.jsonl"
    emitter = LogEmitter(EmitterConfig(log_path=log_path))
    gateway = build_default_graph_write_gateway(config)
    record_audit = build_system_audit_recorder(config)
    embed = OneHotEmbeddingCaller()
    run_ids: list[str] = []

    def _merge(instrument_id: str, run_id: str) -> None:
        run_ids.append(run_id)
        with bind_run_context(run_id):
            merge_baseline_graph(
                instrument_id,
                baseline_graph=select_graph(db, baseline_names[instrument_id]),
                single_tenant_graph=graph,
                submitter=gateway,
                single_tenant_graph_name=graph_name,
                baseline_graph_name=baseline_names[instrument_id],
                audit_event_id=record_audit(
                    action="ingestion_run.submit",
                    resource_type="ingestion_run",
                    resource_id=run_id,
                    details={"trigger": "ll11_hermetic"},
                ),
                embed_model="fake-embed-model",
                merge_threshold=MERGE_THRESHOLD,
                review_threshold=REVIEW_THRESHOLD,
                role_review_threshold=ROLE_REVIEW_THRESHOLD,
                call_embedding=embed,
                emitter=emitter,
            )

    try:
        for instrument_id, tag in ((EXISTING_ID, "Existing"), (NEW_ID, "New")):
            _seed_baseline(select_graph(db, baseline_names[instrument_id]), instrument_id, tag)
        _merge(EXISTING_ID, f"ll11-{suffix}-existing")
        before = _snapshot(graph)
        _merge(NEW_ID, f"ll11-{suffix}-new-pass1")
        after_pass_1 = _snapshot(graph)
        _merge(NEW_ID, f"ll11-{suffix}-new-pass2")
        after_pass_2 = _snapshot(graph)
        emitter.flush()
        yield MergedGraphRun(
            graph=graph,
            graph_name=graph_name,
            before=before,
            after_pass_1=after_pass_1,
            after_pass_2=after_pass_2,
            run_ids=run_ids,
            log_path=log_path,
            shared_capability_id=capability_id(SHARED_CAPABILITY),
        )
    finally:
        emitter.stop()
        existing = set(db.list_graphs())
        for name in (graph_name, *baseline_names.values()):
            if name in existing:
                db.select_graph(name).delete()
