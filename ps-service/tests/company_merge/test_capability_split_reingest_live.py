"""After a split, a re-ingest and a restore resolve the split Capability exactly, on a real graph.

`falkordb_live`, no LLM. The real `merge_baseline_graph`, the real split writer and the real
restore `policy_system` leg run over a uniquely named single-tenant graph on FalkorDB, writing
through a real `GraphWriteGateway` (over the in-memory log store), with the scripted Capability
vectors of `_resolution_fixtures`. The runs, in order:

1. an origin regulation mints Capability `A`;
2. the first version resolves its Capability `X` onto `A` by similarity and records
   `ResolvedCapability {id: X, resolves_to: A}`;
3. the split group mints `X` from the first version's baseline graph and deletes the marker;
4. another regulation and the amended version regenerate `X` through Company Merge;
5. a restore's `policy_system` leg regenerates `X` and is submitted as one group.

Every run after the split must resolve `X` `exact`: one Capability `X`, no `ResolvedCapability`
and no `PendingReview` naming `X`, and each regenerating Obligation `REQUIRES` `X`. The fast tests
read in-memory doubles that key nodes by label and id; these read the graph with real Cypher, so a
label or direction that is wrong in the production statements fails here.

Run against a throwaway server, for example:
`PS_TEST_FALKORDB_HOST=127.0.0.1 PS_TEST_FALKORDB_PORT=16379
uv run pytest ps-service/tests/company_merge/test_capability_split_reingest_live.py
-m falkordb_live -q`.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from graph_gateway._fakes import InMemoryGraphLogStore
from graph_gateway.live_endpoints import FALKORDB_TEST_TIMEOUTS, falkordb_endpoint

from company_merge._resolution_fixtures import (
    AMENDED_VERSION,
    EMBED_MODEL,
    FIRST_VERSION,
    MERGE,
    RESOLVED_ID,
    RESOLVED_NAME,
    RESOLVED_VECTOR,
    TARGET_ID,
    TARGET_NAME,
    ByNameEmbedding,
    obligation_of,
    requiring_baseline,
    version_baseline,
)
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from company_merge.test_resolved_capability_amendment_live import (
    ORIGIN_INSTRUMENT,
    _seed_baseline,  # pyright: ignore[reportPrivateUsage] -- the Domain Mapper's baseline shape, written once
)
from ps_service.company_merge.dedup import DEDUP_DECISION_ACTION
from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.domain_mapper.identity import obligation_id, role_id
from ps_service.export.models import (
    SerializedEdge,
    SerializedGraph,
    SerializedNode,
    SerializedPropertyValue,
)
from ps_service.graph_cleanup.graph_submission import GraphWriteTarget, cleanup_submitter
from ps_service.graph_cleanup.group_builders import write_capability_split
from ps_service.graph_cleanup.split_planner import plan_capability_split
from ps_service.graph_cleanup.split_reader import read_capability_split_state
from ps_service.graph_gateway.connection import connect, select_graph
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.models import MutationGroup, UpsertNode
from ps_service.logging import configure
from ps_service.restore.merge_primitives import build_policy_system_leg

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from company_merge._gateway_fakes import FamilyABaseline
    from ps_service.graph_gateway.connection import WritableGraph as GraphHandle
    from ps_service.graph_gateway.models import Primitive

pytestmark = pytest.mark.falkordb_live

_FRESH_INSTRUMENT = "FRESH-1.0"
_RESTORED_INSTRUMENT = "RESTORED-1.0"
_REGENERATING_RUNS = ("fresh", "amended")


@dataclass(frozen=True)
class SplitReingestRun:
    """What the runs left: the graph, the log entries and what was read or built between runs."""

    graph: GraphHandle
    log_path: Path
    capability_x_after_split: list[list[object]]
    restore_leg: tuple[Primitive, ...]
    restore_semantic_resolutions: int
    embedded_after_split: list[str]


def _rows(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query, params or {}).result_set)


def _restored_obligation() -> str:
    role = role_id("Operator", _RESTORED_INSTRUMENT)
    return obligation_id(role, f"Report incidents under {_RESTORED_INSTRUMENT}.")


def _restored_baseline() -> SerializedGraph:
    """A curated baseline of another regulation whose one Obligation `REQUIRES` `X`."""
    role = role_id("Operator", _RESTORED_INSTRUMENT)
    obligation = _restored_obligation()

    def edge(kind: str, source: tuple[str, str], target: tuple[str, str]) -> SerializedEdge:
        properties: dict[str, SerializedPropertyValue] = (
            {"source_ref": "Art. 1"} if kind == "DEFINES" else {}
        )
        return SerializedEdge(kind, source[0], source[1], target[0], target[1], properties)

    return SerializedGraph(
        nodes=(
            SerializedNode(
                "RegulatoryInstrument", {"id": _RESTORED_INSTRUMENT, "title": _RESTORED_INSTRUMENT}
            ),
            SerializedNode("Role", {"id": role, "name": "Operator", "confidence": 0.9}),
            SerializedNode(
                "Obligation",
                {
                    "id": obligation,
                    "text": f"Report incidents under {_RESTORED_INSTRUMENT}.",
                    "confidence": 0.9,
                },
            ),
            SerializedNode(
                "Capability",
                {
                    "id": RESOLVED_ID,
                    "name": RESOLVED_NAME,
                    "confidence": 0.8,
                    "embedding": list(RESOLVED_VECTOR),
                },
            ),
        ),
        edges=(
            edge("DEFINES", ("RegulatoryInstrument", _RESTORED_INSTRUMENT), ("Role", role)),
            edge("HAS", ("Role", role), ("Obligation", obligation)),
            edge("REQUIRES", ("Obligation", obligation), ("Capability", RESOLVED_ID)),
        ),
    )


@pytest.fixture(scope="module")
def split_reingest_run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SplitReingestRun]:
    host, port = falkordb_endpoint()
    db = connect(host=host, port=port, **FALKORDB_TEST_TIMEOUTS)
    suffix = uuid.uuid4().hex[:10]
    graph_name = f"split_reingest_{suffix}_policy_system"
    graph = select_graph(db, graph_name)
    log_store = InMemoryGraphLogStore()
    gateway = GraphWriteGateway(log_store=log_store, graph_opener=lambda g: select_graph(db, g))
    log_path = tmp_path_factory.mktemp("split_reingest") / "merge.jsonl"
    # Graph Cleanup logs to the process default emitter, Company Merge to the one it is handed;
    # installing one emitter as both sends every entry of the runs to `log_path`.
    emitter = configure(log_path)
    caller = ByNameEmbedding()
    baseline_names: list[str] = []

    def _audit_event() -> str:
        audit_event_id = str(uuid.uuid4())
        log_store.audit_rows.add(audit_event_id)
        return audit_event_id

    def _merge(run: str, baseline: FamilyABaseline) -> str:
        baseline_name = f"split_reingest_{suffix}_{run}_baseline"
        baseline_names.append(baseline_name)
        _seed_baseline(select_graph(db, baseline_name), baseline)
        merge_baseline_graph(
            baseline.instrument_id,
            baseline_graph=select_graph(db, baseline_name),
            single_tenant_graph=graph,
            submitter=gateway,
            single_tenant_graph_name=graph_name,
            baseline_graph_name=baseline_name,
            audit_event_id=_audit_event(),
            embed_model=EMBED_MODEL,
            merge_threshold=MERGE,
            review_threshold=REVIEW_THRESHOLD,
            role_review_threshold=ROLE_REVIEW_THRESHOLD,
            call_embedding=caller,
            emitter=emitter,
        )
        return baseline_name

    try:
        _merge("origin", requiring_baseline(ORIGIN_INSTRUMENT, TARGET_ID, TARGET_NAME))
        first_baseline = _merge("first", version_baseline(FIRST_VERSION))
        baselines = ((first_baseline, select_graph(db, first_baseline)),)
        state = read_capability_split_state(graph, baselines, RESOLVED_ID)
        write_capability_split(
            graph,
            baselines,
            GraphWriteTarget(cleanup_submitter(gateway), graph_name),
            audit_event_id=_audit_event(),
            x_id=RESOLVED_ID,
            state_digest=plan_capability_split(state).state_digest,
            created_at=datetime.now(UTC).isoformat(),
        )
        capability_x_after_split = _rows(
            graph, "MATCH (c:Capability {id: $id}) RETURN properties(c)", {"id": RESOLVED_ID}
        )
        embedded_before = len(caller.calls)
        _merge("fresh", version_baseline(_FRESH_INSTRUMENT))
        _merge("amended", version_baseline(AMENDED_VERSION))
        leg = build_policy_system_leg(
            _restored_baseline(),
            instrument_id=_RESTORED_INSTRUMENT,
            live=graph,
            governance=GovernanceImport(),
            merge_threshold=MERGE,
            review_threshold=REVIEW_THRESHOLD,
            role_review_threshold=ROLE_REVIEW_THRESHOLD,
            emitter=emitter,
        )
        gateway.submit_group(
            MutationGroup(
                graph=graph_name, audit_event_id=_audit_event(), primitives=leg.primitives
            )
        )
        emitter.flush()
        yield SplitReingestRun(
            graph=graph,
            log_path=log_path,
            capability_x_after_split=capability_x_after_split,
            restore_leg=leg.primitives,
            restore_semantic_resolutions=len(leg.semantic_resolutions),
            embedded_after_split=caller.calls[embedded_before:],
        )
    finally:
        emitter.stop()
        gateway.stop_reconciler()
        existing = set(db.list_graphs())
        for name in (graph_name, *baseline_names):
            if name in existing:
                db.select_graph(name).delete()


def _x_decisions(run: SplitReingestRun) -> list[object]:
    entries = [
        json.loads(line) for line in run.log_path.read_text(encoding="utf-8").splitlines() if line
    ]
    return [
        entry["outcome"]
        for entry in entries
        if entry.get("action") == DEDUP_DECISION_ACTION and entry.get("entity_id") == RESOLVED_ID
    ]


def test_live_split_leaves_one_capability_x_and_no_marker_after_every_regenerating_run(
    split_reingest_run: SplitReingestRun,
) -> None:
    graph = split_reingest_run.graph

    assert _rows(graph, "MATCH (c:Capability {id: $id}) RETURN count(c)", {"id": RESOLVED_ID}) == [
        [1]
    ]
    assert _rows(
        graph, "MATCH (c:Capability {id: $id}) RETURN properties(c)", {"id": RESOLVED_ID}
    ) == (split_reingest_run.capability_x_after_split)
    assert _rows(
        graph, "MATCH (m:ResolvedCapability {id: $id}) RETURN count(m)", {"id": RESOLVED_ID}
    ) == [[0]]
    assert _rows(
        graph,
        "MATCH (p:PendingReview) WHERE p.incoming_id = $id OR p.nearest_existing_id = $id "
        "RETURN count(p)",
        {"id": RESOLVED_ID},
    ) == [[0]]
    assert RESOLVED_NAME not in split_reingest_run.embedded_after_split


def test_live_each_regenerating_obligation_requires_x(
    split_reingest_run: SplitReingestRun,
) -> None:
    graph = split_reingest_run.graph
    obligations = (
        obligation_of(version_baseline(_FRESH_INSTRUMENT)),
        obligation_of(version_baseline(AMENDED_VERSION)),
        _restored_obligation(),
    )

    for obligation in obligations:
        assert _rows(
            graph,
            "MATCH (:Obligation {id: $id})-[:REQUIRES]->(c:Capability) RETURN c.id",
            {"id": obligation},
        ) == [[RESOLVED_ID]]


def test_live_company_merge_logs_x_as_exact_after_the_split(
    split_reingest_run: SplitReingestRun,
) -> None:
    assert _x_decisions(split_reingest_run) == ["semantic", *("exact" for _ in _REGENERATING_RUNS)]


def test_live_the_restore_leg_resolves_x_without_a_marker_or_a_review(
    split_reingest_run: SplitReingestRun,
) -> None:
    written_labels = {
        primitive.label
        for primitive in split_reingest_run.restore_leg
        if isinstance(primitive, UpsertNode)
    }

    assert split_reingest_run.restore_semantic_resolutions == 0
    assert "ResolvedCapability" not in written_labels
    assert "PendingReview" not in written_labels
    assert ("Capability", RESOLVED_ID) not in {
        (primitive.label, primitive.id)
        for primitive in split_reingest_run.restore_leg
        if isinstance(primitive, UpsertNode)
    }
