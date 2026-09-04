"""Live FalkorDB proof for `restore_instrument`'s native leg (PLAN.md Slice 5.5, D20).

Real FalkorDB, a `RestoreArtifact` with a valid native graph and an
EMPTY baseline graph (this slice's own "baseline processing stubbed as a
no-op" scope -- Slice 5.6 is what exercises a real baseline merge) --
`restore_instrument` stages the native blob (`staging.stage_graph`) and, on
the finalize path (`staging.stage_and_finalize_policy_system_leg`, CHANGES.md
B1), installs it at `{short}_native`; the restored graph is queryable with
exactly the same content as the original artifact (D20: a straight load, no
dedup step at all for this leg).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.export.serialize import serialize_graph, to_json_bytes
from ps_service.restore.restore_instrument import restore_instrument
from restore._fixtures import build_restore_artifact

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB

_ACTOR = "test-actor"
_SIMILARITY_THRESHOLD = 0.9


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


def _empty_baseline_graph() -> SerializedGraph:
    return SerializedGraph(nodes=(), edges=())


@pytest.mark.falkordb_live
def test_restore_instrument_installs_native_leg_with_original_content(
    live_falkordb: FalkorDB, make_emitter: MakeEmitter
) -> None:
    emitter, _log_path = make_emitter()
    token = uuid.uuid4().hex[:12]
    short_name = f"RT55{token}"
    single_tenant_graph_name = f"__ac66_slice55_single_tenant_{token}__"
    native_target = f"{short_name}_native"
    baseline_target = f"{short_name}_baseline"
    native_graph = _native_graph()
    artifact = build_restore_artifact(
        instrument_id=f"RT55-{token}",
        short_name=short_name,
        native_graph=native_graph,
        baseline_graph=_empty_baseline_graph(),
    )

    try:
        outcome = restore_instrument(
            artifact,
            db=live_falkordb,
            single_tenant_graph_name=single_tenant_graph_name,
            similarity_threshold=_SIMILARITY_THRESHOLD,
            actor=_ACTOR,
            emitter=emitter,
        )

        assert outcome.instrument_id == artifact.manifest.instrument_id
        assert live_falkordb.connection.exists(native_target) == 1

        restored_native = serialize_graph(graph_query_handle(live_falkordb, native_target))
        assert to_json_bytes(restored_native) == to_json_bytes(native_graph)
    finally:
        live_falkordb.connection.delete(native_target, baseline_target, single_tenant_graph_name)
