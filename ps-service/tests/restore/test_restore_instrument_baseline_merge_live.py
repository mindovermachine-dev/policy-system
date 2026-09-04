"""Live FalkorDB proof for `restore_instrument`'s baseline leg (PLAN.md Slice 5.6, D6/D8 step 5).

Real FalkorDB, a single-tenant graph pre-seeded with one existing
Capability; restoring an instrument whose baseline Capability set includes
one exact-match (the pre-seeded id) and one genuinely-new Capability results
in: the finalized single-tenant graph gaining the new Capability node and
the expected `DEFINES`/`EXPRESSES`/`HAS`/`SATISFIED_BY`/`REQUIRES` edges, via
the EXISTING, unmodified `graph_writer.persist_*` functions (reused, not
reimplemented -- `restore_instrument._run_baseline_merge` calls exactly
these, in `merge.py::merge_baseline_graph`'s own write order) and the
EXISTING, unmodified `resolve_capability_convergence_offline` (Slices
5.3/5.4, D6) for the dedup decision itself.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.restore.restore_instrument import restore_instrument
from restore._fixtures import build_restore_artifact

if TYPE_CHECKING:
    from collections.abc import Callable

    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB

_ACTOR = "test-actor"
_SIMILARITY_THRESHOLD = 0.9
_EXISTING_CAPABILITY_ID = "cap_existing_encryption"
_NEW_CAPABILITY_ID = "cap_new_key_rotation"


def _baseline_graph(instrument_id: str) -> SerializedGraph:
    role_id = "role_data_controller"
    requirement_id = f"{instrument_id}_req_art_1"
    obligation_id = "obligation_secure_data"
    return SerializedGraph(
        nodes=(
            SerializedNode(
                label="RegulatoryInstrument",
                properties={"id": instrument_id, "title": "RT56 Regulation"},
            ),
            SerializedNode(
                label="Role",
                properties={"id": role_id, "name": "Data Controller", "confidence": 0.9},
            ),
            SerializedNode(
                label="Requirement",
                properties={
                    "id": requirement_id,
                    "text": "Shall secure personal data",
                    "type": "obligation",
                    "confidence": 0.9,
                    "role_id": role_id,
                },
            ),
            SerializedNode(
                label="Obligation",
                properties={"id": obligation_id, "text": "Secure personal data", "confidence": 0.9},
            ),
            SerializedNode(
                label="Capability",
                properties={"id": _EXISTING_CAPABILITY_ID, "name": "Encryption", "confidence": 0.9},
            ),
            SerializedNode(
                label="Capability",
                properties={"id": _NEW_CAPABILITY_ID, "name": "Key Rotation", "confidence": 0.9},
            ),
        ),
        edges=(
            SerializedEdge(
                relationship_type="DEFINES",
                source_label="RegulatoryInstrument",
                source_id=instrument_id,
                target_label="Role",
                target_id=role_id,
                properties={"source_ref": "art. 1"},
            ),
            SerializedEdge(
                relationship_type="EXPRESSES",
                source_label="RegulatoryInstrument",
                source_id=instrument_id,
                target_label="Requirement",
                target_id=requirement_id,
                properties={"source_ref": "art. 1"},
            ),
            SerializedEdge(
                relationship_type="HAS",
                source_label="Role",
                source_id=role_id,
                target_label="Obligation",
                target_id=obligation_id,
                properties={},
            ),
            SerializedEdge(
                relationship_type="SATISFIED_BY",
                source_label="Requirement",
                source_id=requirement_id,
                target_label="Obligation",
                target_id=obligation_id,
                properties={},
            ),
            SerializedEdge(
                relationship_type="REQUIRES",
                source_label="Obligation",
                source_id=obligation_id,
                target_label="Capability",
                target_id=_EXISTING_CAPABILITY_ID,
                properties={},
            ),
            SerializedEdge(
                relationship_type="REQUIRES",
                source_label="Obligation",
                source_id=obligation_id,
                target_label="Capability",
                target_id=_NEW_CAPABILITY_ID,
                properties={},
            ),
        ),
    )


def _empty_native_graph() -> SerializedGraph:
    return SerializedGraph(nodes=(), edges=())


@pytest.mark.falkordb_live
def test_restore_instrument_merges_new_capability_and_rewires_edges(
    live_falkordb: FalkorDB,
    make_emitter: MakeEmitter,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    emitter, _log_path = make_emitter()
    token = uuid.uuid4().hex[:12]
    short_name = f"RT56{token}"
    single_tenant_graph_name = f"__ac66_slice56_single_tenant_{token}__"
    native_target = f"{short_name}_native"
    baseline_target = f"{short_name}_baseline"
    instrument_id = f"RT56-{token}"

    live_falkordb.select_graph(single_tenant_graph_name).query(
        "CREATE (:Capability {id: $id, name: 'Encryption'})", {"id": _EXISTING_CAPABILITY_ID}
    )

    artifact = build_restore_artifact(
        instrument_id=instrument_id,
        short_name=short_name,
        native_graph=_empty_native_graph(),
        baseline_graph=_baseline_graph(instrument_id),
    )

    try:
        restore_instrument(
            artifact,
            db=live_falkordb,
            single_tenant_graph_name=single_tenant_graph_name,
            similarity_threshold=_SIMILARITY_THRESHOLD,
            actor=_ACTOR,
            emitter=emitter,
        )

        capability_rows = query_result_rows(
            live_falkordb,
            single_tenant_graph_name,
            "MATCH (c:Capability) RETURN c.id, c.name ORDER BY c.id",
        )
        assert capability_rows == [
            [_EXISTING_CAPABILITY_ID, "Encryption"],  # exact match: untouched, no overwrite
            [_NEW_CAPABILITY_ID, "Key Rotation"],  # newly minted
        ]

        requires_rows = query_result_rows(
            live_falkordb,
            single_tenant_graph_name,
            "MATCH (o:Obligation)-[:REQUIRES]->(c:Capability) RETURN o.id, c.id ORDER BY c.id",
        )
        assert requires_rows == [
            ["obligation_secure_data", _EXISTING_CAPABILITY_ID],
            ["obligation_secure_data", _NEW_CAPABILITY_ID],
        ]

        has_rows = query_result_rows(
            live_falkordb,
            single_tenant_graph_name,
            "MATCH (r:Role)-[:HAS]->(o:Obligation) RETURN r.id, o.id",
        )
        assert has_rows == [["role_data_controller", "obligation_secure_data"]]

        satisfied_by_rows = query_result_rows(
            live_falkordb,
            single_tenant_graph_name,
            "MATCH (req:Requirement)-[:SATISFIED_BY]->(o:Obligation) RETURN req.id, o.id",
        )
        assert satisfied_by_rows == [[f"{instrument_id}_req_art_1", "obligation_secure_data"]]

        defines_rows = query_result_rows(
            live_falkordb,
            single_tenant_graph_name,
            "MATCH (ri:RegulatoryInstrument)-[:DEFINES]->(r:Role) RETURN ri.id, r.id",
        )
        assert defines_rows == [[instrument_id, "role_data_controller"]]
    finally:
        live_falkordb.connection.delete(native_target, baseline_target, single_tenant_graph_name)
