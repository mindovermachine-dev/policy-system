"""An amended version keeps its Capability on the first semantic resolution, on a real graph.

`falkordb_live` + `postgres_live`, no LLM. The real `merge_baseline_graph` runs over a uniquely
named single-tenant graph on FalkorDB, writing through the real Graph Write Gateway on a scratch
PS Postgres, with the scripted Capability vectors of `_resolution_fixtures`. The runs, in order:

1. an origin regulation mints Capability `A`;
2. the first version resolves its Capability `X` onto `A` by similarity and records
   `ResolvedCapability {id: X, resolves_to: A}`;
3. the first version again, which must leave exactly one marker;
4. an unrelated regulation mints `B`, closer to `X` than `A` is;
5. the amended version, which regenerates `X` and must follow the marker to `A`.

The fast tests read in-memory doubles that key nodes by label and id; these read the graph with
real Cypher and the gateway log with the real store, so a label or a group that is wrong in the
production statements fails here.

Run against throwaway servers, for example:
`PS_FALKORDB_HOST=127.0.0.1 PS_FALKORDB_PORT=16379
PS_TEST_FALKORDB_HOST=127.0.0.1 PS_TEST_FALKORDB_PORT=16379
PS_TEST_POSTGRES_SUPERUSER_DSN=postgresql://postgres@127.0.0.1:54329/postgres
uv run pytest ps-service/tests/company_merge/test_resolved_capability_amendment_live.py
-m "falkordb_live and postgres_live"`.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest
from graph_gateway.live_endpoints import FALKORDB_TEST_TIMEOUTS, falkordb_endpoint

from company_merge._resolution_fixtures import (
    AMENDED_VERSION,
    EMBED_MODEL,
    FIRST_VERSION,
    MERGE,
    NEWER_ID,
    RESOLVED_ID,
    RESOLVED_NAME,
    TARGET_ID,
    TARGET_NAME,
    ByNameEmbedding,
    obligation_of,
    requiring_baseline,
    unrelated_baseline,
    version_baseline,
)
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from ps_service.company_merge.dedup import read_existing_canonical_index
from ps_service.company_merge.graph_writer import ANY_LABEL_EXISTENCE_READ
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.reviewable_kinds import CAPABILITY
from ps_service.graph_gateway.connection import connect, select_graph
from ps_service.graph_gateway.default_gateway import build_default_graph_write_gateway
from ps_service.graph_gateway.store import PsycopgGraphLogStore
from ps_service.graph_gateway.system_audit import build_system_audit_recorder
from ps_service.logging import EmitterConfig, LogEmitter, bind_run_context

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persistence.provisioned_postgres import Provisioned

    from company_merge._gateway_fakes import FamilyABaseline
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.graph_gateway.models import GraphLogGroup

pytestmark = [pytest.mark.falkordb_live, pytest.mark.postgres_live]

ORIGIN_INSTRUMENT = "ORIGIN-1.0"
_MARKER = "ResolvedCapability"

_BASELINE_NODE_QUERIES = {
    "Role": "MERGE (n:Role {id: $id}) SET n.name = $text, n.confidence = $confidence",
    "Obligation": "MERGE (n:Obligation {id: $id}) SET n.text = $text, n.confidence = $confidence",
    "Capability": "MERGE (n:Capability {id: $id}) SET n.name = $text, n.confidence = $confidence",
}
_BASELINE_EDGE_QUERIES = {
    "HAS": "MATCH (s:Role {id: $source}), (t:Obligation {id: $target}) MERGE (s)-[:HAS]->(t)",
    "REQUIRES": (
        "MATCH (s:Obligation {id: $source}), (t:Capability {id: $target}) "
        "MERGE (s)-[:REQUIRES]->(t)"
    ),
}
_MARKER_TARGETS = "MATCH (m:ResolvedCapability {id: $id}) RETURN m.resolves_to"
_REQUIRES_TARGETS = "MATCH (:Obligation {id: $id})-[:REQUIRES]->(c) RETURN c.id ORDER BY c.id"
_EDGES_TOUCHING = (
    "MATCH (a)-[r]->(b) WHERE a.id = $id OR b.id = $id RETURN type(r), a.id, b.id, properties(r)"
)


@dataclass(frozen=True)
class AmendmentRun:
    """What the five runs left: the graph, the gateway log and what was read between runs."""

    graph: GraphHandle
    graph_name: str
    log_store: PsycopgGraphLogStore
    audit_event_ids: dict[str, str]
    markers_after_first_version: list[list[object]]
    markers_after_rerun: list[list[object]]
    first_version_edges_before_amendment: list[tuple[str, ...]]
    embedded_before_amendment: list[str]
    embedded_by_amendment: list[str]


def _rows(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query, params or {}).result_set)


def _edges_touching(graph: GraphHandle, node_id: str) -> list[tuple[str, ...]]:
    """Every edge at `node_id` as (type, source id, target id, properties), in a stable order."""
    return sorted(
        (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            repr(sorted(cast("dict[str, object]", row[3]).items())),
        )
        for row in _rows(graph, _EDGES_TOUCHING, {"id": node_id})
    )


def _seed_baseline(graph: GraphHandle, baseline: FamilyABaseline) -> None:
    """Write one regulation's `{short}_baseline` graph as the Domain Mapper leaves it."""
    graph.query(
        "MERGE (n:RegulatoryInstrument {id: $id}) SET n.title = $id",
        {"id": baseline.instrument_id},
    )
    nodes = (
        *(("Role", node_id, name, 0.9) for node_id, name in baseline.roles.items()),
        *(("Obligation", node_id, *text) for node_id, text in baseline.obligations.items()),
        *(("Capability", node_id, *name) for node_id, name in baseline.capabilities.items()),
    )
    for label, node_id, text, confidence in nodes:
        graph.query(
            _BASELINE_NODE_QUERIES[label],
            {"id": node_id, "text": text, "confidence": confidence},
        )
    for edge_type, _, source, _, target in baseline.edges:
        graph.query(_BASELINE_EDGE_QUERIES[edge_type], {"source": source, "target": target})


@pytest.fixture(scope="module")
def amendment_run(
    provisioned_graph_log: Provisioned, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[AmendmentRun]:
    host, port = falkordb_endpoint()
    config = dataclasses.replace(
        provisioned_graph_log.state_config(), falkordb_host=host, falkordb_port=port
    )
    db = connect(host=host, port=port, **FALKORDB_TEST_TIMEOUTS)
    suffix = uuid.uuid4().hex[:10]
    graph_name = f"resolved_amendment_{suffix}_policy_system"
    graph = select_graph(db, graph_name)
    baseline_names: list[str] = []
    emitter = LogEmitter(
        EmitterConfig(log_path=tmp_path_factory.mktemp("resolved_amendment") / "merge.jsonl")
    )
    gateway = build_default_graph_write_gateway(config)
    record_audit = build_system_audit_recorder(config)
    caller = ByNameEmbedding()
    audit_event_ids: dict[str, str] = {}

    def _merge(run: str, baseline: FamilyABaseline) -> None:
        baseline_name = f"resolved_amendment_{suffix}_{run}_baseline"
        baseline_names.append(baseline_name)
        _seed_baseline(select_graph(db, baseline_name), baseline)
        run_id = f"resolved-amendment-{suffix}-{run}"
        audit_event_ids[run] = record_audit(
            action="ingestion_run.submit",
            resource_type="ingestion_run",
            resource_id=run_id,
            details={"trigger": "resolved_amendment_live"},
        )
        with bind_run_context(run_id):
            merge_baseline_graph(
                baseline.instrument_id,
                baseline_graph=select_graph(db, baseline_name),
                single_tenant_graph=graph,
                submitter=gateway,
                single_tenant_graph_name=graph_name,
                baseline_graph_name=baseline_name,
                audit_event_id=audit_event_ids[run],
                embed_model=EMBED_MODEL,
                merge_threshold=MERGE,
                review_threshold=REVIEW_THRESHOLD,
                role_review_threshold=ROLE_REVIEW_THRESHOLD,
                call_embedding=caller,
                emitter=emitter,
            )

    try:
        _merge("origin", requiring_baseline(ORIGIN_INSTRUMENT, TARGET_ID, TARGET_NAME))
        _merge("first", version_baseline(FIRST_VERSION))
        markers_after_first_version = _rows(graph, _MARKER_TARGETS, {"id": RESOLVED_ID})
        _merge("rerun", version_baseline(FIRST_VERSION))
        markers_after_rerun = _rows(graph, _MARKER_TARGETS, {"id": RESOLVED_ID})
        _merge("unrelated", unrelated_baseline())
        first_obligation = obligation_of(version_baseline(FIRST_VERSION))
        edges_before = _edges_touching(graph, first_obligation)
        embedded_before = list(caller.calls)
        _merge("amended", version_baseline(AMENDED_VERSION))
        emitter.flush()
        yield AmendmentRun(
            graph=graph,
            graph_name=graph_name,
            log_store=PsycopgGraphLogStore(config),
            audit_event_ids=audit_event_ids,
            markers_after_first_version=markers_after_first_version,
            markers_after_rerun=markers_after_rerun,
            first_version_edges_before_amendment=edges_before,
            embedded_before_amendment=embedded_before,
            embedded_by_amendment=caller.calls[len(embedded_before) :],
        )
    finally:
        emitter.stop()
        existing = set(db.list_graphs())
        for name in (graph_name, *baseline_names):
            if name in existing:
                db.select_graph(name).delete()


def _groups_of(run: AmendmentRun, name: str) -> tuple[GraphLogGroup, ...]:
    return tuple(
        group
        for group in run.log_store.read_groups_by_audit_event(run.audit_event_ids[name])
        if group.graph == run.graph_name
    )


def test_live_amendment_requires_the_first_resolution_target_and_never_the_newer_capability(
    amendment_run: AmendmentRun,
) -> None:
    graph = amendment_run.graph
    amended_obligation = obligation_of(version_baseline(AMENDED_VERSION))
    first_obligation = obligation_of(version_baseline(FIRST_VERSION))
    unrelated_obligation = obligation_of(unrelated_baseline())

    assert _rows(graph, "MATCH (c:Capability {id: $id}) RETURN c.id", {"id": NEWER_ID}) == [
        [NEWER_ID]
    ]
    assert _rows(graph, _REQUIRES_TARGETS, {"id": amended_obligation}) == [[TARGET_ID]]
    assert _rows(
        graph,
        "MATCH (o:Obligation)-[:REQUIRES]->(:Capability {id: $id}) RETURN o.id ORDER BY o.id",
        {"id": NEWER_ID},
    ) == [[unrelated_obligation]]
    assert _rows(graph, "MATCH (c:Capability {id: $id}) RETURN count(c)", {"id": RESOLVED_ID}) == [
        [0]
    ]
    assert RESOLVED_NAME in amendment_run.embedded_before_amendment
    assert RESOLVED_NAME not in amendment_run.embedded_by_amendment
    assert _rows(
        graph,
        "MATCH (p:PendingReview) WHERE p.incoming_id = $id RETURN count(p)",
        {"id": RESOLVED_ID},
    ) == [[0]]
    assert amendment_run.first_version_edges_before_amendment
    assert _edges_touching(graph, first_obligation) == (
        amendment_run.first_version_edges_before_amendment
    )


def test_live_marker_is_one_node_and_is_not_a_capability(amendment_run: AmendmentRun) -> None:
    graph = amendment_run.graph

    assert amendment_run.markers_after_first_version == [[TARGET_ID]]
    assert amendment_run.markers_after_rerun == [[TARGET_ID]]
    assert _rows(graph, "MATCH (n {id: $id}) RETURN labels(n)", {"id": RESOLVED_ID}) == [
        [[_MARKER]]
    ]
    capability_ids = {row[0] for row in _rows(graph, "MATCH (c:Capability) RETURN c.id")}
    assert TARGET_ID in capability_ids
    assert RESOLVED_ID not in capability_ids
    candidates = {node.id for node in read_existing_canonical_index(graph, CAPABILITY)}
    assert TARGET_ID in candidates
    assert RESOLVED_ID not in candidates
    assert _rows(graph, ANY_LABEL_EXISTENCE_READ, {"ids": [RESOLVED_ID]}) == []
    assert _rows(graph, ANY_LABEL_EXISTENCE_READ, {"ids": [RESOLVED_ID, TARGET_ID]}) == [
        [TARGET_ID]
    ]


def test_live_marker_and_requires_edge_share_one_graph_log_group(
    amendment_run: AmendmentRun,
) -> None:
    first_obligation = obligation_of(version_baseline(FIRST_VERSION))
    requires_identity = f"REQUIRES:{first_obligation}->{TARGET_ID}"

    groups = _groups_of(amendment_run, "first")
    marker_groups = [
        group
        for group in groups
        for entry in group.entries
        if entry.name == _MARKER and entry.identity == RESOLVED_ID
    ]
    assert len(marker_groups) == 1
    [marker_group] = marker_groups
    [marker_entry] = [entry for entry in marker_group.entries if entry.name == _MARKER]
    assert marker_entry.content == {"op": "upsert_node", "properties": {"resolves_to": TARGET_ID}}
    assert ("REQUIRES", requires_identity) in {
        (entry.name, entry.identity) for entry in marker_group.entries
    }
    assert not [
        entry
        for group in _groups_of(amendment_run, "rerun")
        for entry in group.entries
        if entry.name == _MARKER
    ]
