"""A restored amended version keeps its Capability on the first semantic resolution, live.

`falkordb_live` + `postgres_live`. The real `restore_instrument` runs three times against the real
gateway (graph log on a scratch PS Postgres, FalkorDB) into a uniquely named single-tenant graph
that already holds Capability `A`: the first version, whose `X` resolves onto `A` by similarity
and records `ResolvedCapability {id: X, resolves_to: A}`; an unrelated regulation that brings `B`,
closer to `X` than `A` is; then the amended version, which regenerates `X` and must follow the
marker to `A`. Restore scores from the embeddings the artifact carries, so no embedding call is
involved. The graph is read with real Cypher, which the label-and-id keyed doubles cannot fake.

Run against throwaway servers, for example:
`PS_FALKORDB_HOST=127.0.0.1 PS_FALKORDB_PORT=16379
PS_TEST_POSTGRES_SUPERUSER_DSN=postgresql://postgres@127.0.0.1:54329/postgres
uv run pytest ps-service/tests/restore/test_restore_resolution_markers_live.py
-m "falkordb_live and postgres_live"`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

import pytest
from company_merge._resolution_fixtures import (
    AMENDED_VERSION,
    FIRST_VERSION,
    MERGE,
    NEWER_ID,
    NEWER_NAME,
    NEWER_VECTOR,
    RESOLVED_ID,
    RESOLVED_NAME,
    RESOLVED_VECTOR,
    TARGET_ID,
    TARGET_NAME,
    TARGET_VECTOR,
    UNRELATED_INSTRUMENT,
)
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD

from ps_service.domain_mapper.identity import obligation_id, role_id
from ps_service.export.models import (
    SerializedEdge,
    SerializedGraph,
    SerializedNode,
    SerializedPropertyValue,
)
from ps_service.restore.restore_instrument import restore_instrument
from restore._fixtures import build_restore_artifact
from restore.live_gateway import live_restore

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB
    from persistence.provisioned_postgres import Provisioned

    from ps_service.restore.models import RestoreArtifact

pytestmark = [pytest.mark.falkordb_live, pytest.mark.postgres_live]

_ACTOR = "test-actor"
_SHORT_NAMES = {
    FIRST_VERSION: "RMAMEND1",
    AMENDED_VERSION: "RMAMEND2",
    UNRELATED_INSTRUMENT: "RMOTHER",
}
_EDGES_TOUCHING = (
    "MATCH (a)-[r]->(b) WHERE a.id = $id OR b.id = $id RETURN type(r), a.id, b.id, properties(r)"
)


def _obligation(instrument_id: str) -> str:
    return obligation_id(
        role_id("Operator", instrument_id), f"Report incidents under {instrument_id}."
    )


def _baseline(
    instrument_id: str, capability: str, name: str, vector: tuple[float, ...]
) -> SerializedGraph:
    """One Role of `instrument_id` with one Obligation that `REQUIRES` one embedded Capability."""
    role, obligation = role_id("Operator", instrument_id), _obligation(instrument_id)

    def edge(kind: str, source: tuple[str, str], target: tuple[str, str]) -> SerializedEdge:
        properties: dict[str, SerializedPropertyValue] = (
            {"source_ref": "Art. 1"} if kind == "DEFINES" else {}
        )
        return SerializedEdge(kind, source[0], source[1], target[0], target[1], properties)

    return SerializedGraph(
        nodes=(
            SerializedNode("RegulatoryInstrument", {"id": instrument_id, "title": instrument_id}),
            SerializedNode("Role", {"id": role, "name": "Operator", "confidence": 0.9}),
            SerializedNode(
                "Obligation",
                {
                    "id": obligation,
                    "text": f"Report incidents under {instrument_id}.",
                    "confidence": 0.9,
                },
            ),
            SerializedNode(
                "Capability",
                {"id": capability, "name": name, "confidence": 0.8, "embedding": list(vector)},
            ),
        ),
        edges=(
            edge("DEFINES", ("RegulatoryInstrument", instrument_id), ("Role", role)),
            edge("HAS", ("Role", role), ("Obligation", obligation)),
            edge("REQUIRES", ("Obligation", obligation), ("Capability", capability)),
        ),
    )


class _LiveRestorer:
    """Restores artifacts into one single-tenant graph, each through its own live gateway."""

    def __init__(
        self,
        db: FalkorDB,
        prov: Provisioned,
        graph_name: str,
        make_emitter: MakeEmitter,
        created: list[str],
    ) -> None:
        self._db = db
        self._prov = prov
        self._graph_name = graph_name
        self._make_emitter = make_emitter
        self._token = uuid.uuid4().hex[:8]
        self._created = created

    def artifact(self, instrument_id: str, baseline: SerializedGraph) -> RestoreArtifact:
        short_name = f"{_SHORT_NAMES[instrument_id]}{self._token}"
        self._created += [f"{short_name.lower()}_native", f"{short_name.lower()}_baseline"]
        native = SerializedGraph(
            nodes=(
                SerializedNode(
                    "RegulatoryInstrument", {"id": instrument_id, "title": instrument_id}
                ),
            ),
            edges=(),
        )
        return build_restore_artifact(
            instrument_id=instrument_id,
            short_name=short_name,
            native_graph=native,
            baseline_graph=baseline,
        )

    def restore(self, artifact: RestoreArtifact) -> None:
        emitter, _ = self._make_emitter()
        with live_restore(self._prov, self._db) as live:
            restore_instrument(
                artifact,
                db=self._db,
                single_tenant_graph_name=self._graph_name,
                merge_threshold=MERGE,
                review_threshold=REVIEW_THRESHOLD,
                role_review_threshold=ROLE_REVIEW_THRESHOLD,
                actor=_ACTOR,
                open_gateway=live.open_gateway,
                audit_event_id=live.audit_event_id,
                emitter=emitter,
            )


def _rows(
    db: FalkorDB, graph_name: str, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    return cast("list[list[object]]", db.select_graph(graph_name).query(query, params).result_set)


def _edges_touching(db: FalkorDB, graph_name: str, node_id: str) -> list[tuple[str, ...]]:
    return sorted(
        (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            repr(sorted(cast("dict[str, object]", row[3]).items())),
        )
        for row in _rows(db, graph_name, _EDGES_TOUCHING, {"id": node_id})
    )


def test_live_restore_lands_on_the_recorded_resolution_target(
    live_falkordb: FalkorDB, provisioned_graph_log: Provisioned, make_emitter: MakeEmitter
) -> None:
    graph_name = f"__resolution_marker_restore_{uuid.uuid4().hex[:12]}__"
    live_falkordb.select_graph(graph_name).query(
        "CREATE (:Capability {id: $id, name: $name, embedding: $embedding, status: 'active'})",
        {"id": TARGET_ID, "name": TARGET_NAME, "embedding": list(TARGET_VECTOR)},
    )
    created: list[str] = []
    restorer = _LiveRestorer(
        live_falkordb, provisioned_graph_log, graph_name, make_emitter, created
    )
    try:
        restorer.restore(
            restorer.artifact(
                FIRST_VERSION,
                _baseline(FIRST_VERSION, RESOLVED_ID, RESOLVED_NAME, RESOLVED_VECTOR),
            )
        )
        assert _rows(
            live_falkordb,
            graph_name,
            "MATCH (m:ResolvedCapability {id: $id}) RETURN m.resolves_to",
            {"id": RESOLVED_ID},
        ) == [[TARGET_ID]]
        restorer.restore(
            restorer.artifact(
                UNRELATED_INSTRUMENT,
                _baseline(UNRELATED_INSTRUMENT, NEWER_ID, NEWER_NAME, NEWER_VECTOR),
            )
        )
        first_edges = _edges_touching(live_falkordb, graph_name, _obligation(FIRST_VERSION))

        restorer.restore(
            restorer.artifact(
                AMENDED_VERSION,
                _baseline(AMENDED_VERSION, RESOLVED_ID, RESOLVED_NAME, RESOLVED_VECTOR),
            )
        )

        assert _rows(
            live_falkordb,
            graph_name,
            "MATCH (c:Capability {id: $id}) RETURN c.id",
            {"id": NEWER_ID},
        ) == [[NEWER_ID]]
        assert _rows(
            live_falkordb,
            graph_name,
            "MATCH (:Obligation {id: $id})-[:REQUIRES]->(c) RETURN c.id",
            {"id": _obligation(AMENDED_VERSION)},
        ) == [[TARGET_ID]]
        assert _rows(
            live_falkordb,
            graph_name,
            "MATCH (c:Capability {id: $id}) RETURN count(c)",
            {"id": RESOLVED_ID},
        ) == [[0]]
        assert _rows(
            live_falkordb,
            graph_name,
            "MATCH (m:ResolvedCapability {id: $id}) RETURN m.resolves_to",
            {"id": RESOLVED_ID},
        ) == [[TARGET_ID]]
        assert _rows(
            live_falkordb,
            graph_name,
            "MATCH (p:PendingReview) WHERE p.incoming_id = $id RETURN count(p)",
            {"id": RESOLVED_ID},
        ) == [[0]]
        assert first_edges
        assert _edges_touching(live_falkordb, graph_name, _obligation(FIRST_VERSION)) == first_edges
    finally:
        live_falkordb.connection.delete(graph_name, *created)
