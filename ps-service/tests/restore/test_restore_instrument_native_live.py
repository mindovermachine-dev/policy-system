"""Live FalkorDB proof for `restore_instrument`'s native leg.

Real FalkorDB, a `RestoreArtifact` with a valid native graph and an EMPTY baseline graph --
`restore_instrument` installs the native blob at `{short}_native`; the restored graph is queryable
with exactly the same content as the original artifact (a straight load, no dedup step at all for
this leg).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from company_merge._thresholds import REVIEW_THRESHOLD

from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.export.serialize import serialize_graph, to_json_bytes
from ps_service.restore.leg_primitives import edge_identity
from ps_service.restore.restore_instrument import restore_instrument
from restore._fixtures import build_restore_artifact
from restore.live_gateway import live_restore

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB
    from persistence.provisioned_postgres import Provisioned

_ACTOR = "test-actor"
_MERGE_THRESHOLD = 0.9


def _native_graph() -> SerializedGraph:
    return SerializedGraph(
        nodes=(
            SerializedNode(
                label="RegulatoryInstrument", properties={"id": "RT55-1.0", "title": "RT55"}
            ),
            SerializedNode(
                label="ARTICLE", properties={"id": "RT55-art-1", "text": "Article one text"}
            ),
        ),
        edges=(
            SerializedEdge(
                relationship_type="HAS",
                source_label="RegulatoryInstrument",
                source_id="RT55-1.0",
                target_label="ARTICLE",
                target_id="RT55-art-1",
                properties={},
            ),
        ),
    )


def _with_edge_identities(graph: SerializedGraph) -> SerializedGraph:
    """`graph` with each edge's gateway identity, as a restored graph reads back."""
    return SerializedGraph(
        nodes=graph.nodes,
        edges=tuple(
            SerializedEdge(
                relationship_type=edge.relationship_type,
                source_label=edge.source_label,
                source_id=edge.source_id,
                target_label=edge.target_label,
                target_id=edge.target_id,
                properties={**edge.properties, "identity": edge_identity(edge)},
            )
            for edge in graph.edges
        ),
    )


def _empty_baseline_graph() -> SerializedGraph:
    return SerializedGraph(nodes=(), edges=())


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_restore_instrument_installs_native_leg_with_original_content(
    live_falkordb: FalkorDB, provisioned_graph_log: Provisioned, make_emitter: MakeEmitter
) -> None:
    emitter, _log_path = make_emitter()
    token = uuid.uuid4().hex[:12]
    short_name = f"RT55{token}"
    single_tenant_graph_name = f"__ac66_slice55_single_tenant_{token}__"
    # restore_instrument lowercases short_name to match ingestion/domain_mapper's
    # own {short_name.lower()}_native/_baseline convention -- mirrored here so
    # this test's expected target names match the real, fixed graph keys.
    native_target = f"{short_name.lower()}_native"
    baseline_target = f"{short_name.lower()}_baseline"
    native_graph = _native_graph()
    artifact = build_restore_artifact(
        instrument_id=f"RT55-{token}",
        short_name=short_name,
        native_graph=native_graph,
        baseline_graph=_empty_baseline_graph(),
    )

    try:
        with live_restore(provisioned_graph_log, live_falkordb) as live:
            outcome = restore_instrument(
                artifact,
                db=live_falkordb,
                single_tenant_graph_name=single_tenant_graph_name,
                merge_threshold=_MERGE_THRESHOLD,
                review_threshold=REVIEW_THRESHOLD,
                actor=_ACTOR,
                open_gateway=live.open_gateway,
                audit_event_id=live.audit_event_id,
                emitter=emitter,
            )

        assert outcome.instrument_id == artifact.manifest.instrument_id
        assert live_falkordb.connection.exists(native_target) == 1

        restored_native = serialize_graph(graph_query_handle(live_falkordb, native_target))
        # Restored edges carry the gateway's identity (container architecture: "Restored edges
        # carry an identity"), so the expected graph is the artifact plus that one property.
        assert to_json_bytes(restored_native) == to_json_bytes(_with_edge_identities(native_graph))
    finally:
        live_falkordb.connection.delete(native_target, baseline_target, single_tenant_graph_name)
