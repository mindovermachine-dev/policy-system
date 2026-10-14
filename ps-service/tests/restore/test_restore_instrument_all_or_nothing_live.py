"""Live proof that a failure while building the restore leaves everything untouched.

The REAL `restore_instrument` runs against the real gateway (Postgres graph log, FalkorDB). A
failure injected partway through the `policy_system` leg's build (after the native and baseline
legs were already built) must leave: the graph log empty for the restore's audit event, the live
single-tenant graph byte-for-byte unchanged (compared as `SerializedGraph`s via
`export.serialize.serialize_graph`), and `{short}_native`/`{short}_baseline` NOT existing -- the
"neither graph" requirement across legs, now because nothing is submitted until every leg is
built, not because staged keys are discarded (there are none).

Not run in the implementation sandbox (no FalkorDB, no Postgres); the fast twin is
`test_restore_gateway.py`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

import ps_service.restore.merge_primitives as merge_primitives_module
from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.export.serialize import serialize_graph
from ps_service.restore.restore_instrument import restore_instrument
from restore._fixtures import build_restore_artifact
from restore.live_gateway import live_restore

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB
    from persistence.provisioned_postgres import Provisioned

_ACTOR = "test-actor"
_SIMILARITY_THRESHOLD = 0.9


class _ForcedMergeFailureError(Exception):
    """The build-step failure this test deliberately injects."""


def _native_graph(instrument_id: str) -> SerializedGraph:
    return SerializedGraph(
        nodes=(
            SerializedNode(
                label="RegulatoryInstrument", properties={"id": instrument_id, "title": "RT57"}
            ),
            SerializedNode(
                label="ARTICLE", properties={"id": f"{instrument_id}-art-1", "text": "Article one"}
            ),
        ),
        edges=(
            SerializedEdge(
                relationship_type="HAS",
                source_label="RegulatoryInstrument",
                source_id=instrument_id,
                target_label="ARTICLE",
                target_id=f"{instrument_id}-art-1",
                properties={},
            ),
        ),
    )


def _baseline_graph(instrument_id: str) -> SerializedGraph:
    return SerializedGraph(
        nodes=(
            SerializedNode(
                label="RegulatoryInstrument", properties={"id": instrument_id, "title": "RT57"}
            ),
            SerializedNode(
                label="Capability",
                properties={"id": "cap_5_7_new", "name": "New Capability", "confidence": 0.9},
            ),
        ),
        edges=(),
    )


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_build_step_failure_leaves_the_log_and_every_graph_untouched(
    live_falkordb: FalkorDB,
    provisioned_graph_log: Provisioned,
    make_emitter: MakeEmitter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitter, _log_path = make_emitter()
    token = uuid.uuid4().hex[:12]
    short_name = f"RT57{token}"
    single_tenant_graph_name = f"__ac66_slice57_single_tenant_{token}__"
    # restore_instrument lowercases short_name to match ingestion/domain_mapper's
    # own {short_name.lower()}_native/_baseline convention -- mirrored here so
    # this test's expected target names match the real, fixed graph keys.
    native_target = f"{short_name.lower()}_native"
    baseline_target = f"{short_name.lower()}_baseline"
    instrument_id = f"RT57-{token}"

    # Pre-seed the single-tenant graph with real content, so "unchanged" is a
    # meaningful assertion, not vacuously true of an empty graph.
    live_falkordb.select_graph(single_tenant_graph_name).query(
        "CREATE (:Capability {id: 'cap_pre_existing', name: 'Pre-existing'})"
    )
    before = serialize_graph(graph_query_handle(live_falkordb, single_tenant_graph_name))

    artifact = build_restore_artifact(
        instrument_id=instrument_id,
        short_name=short_name,
        native_graph=_native_graph(instrument_id),
        baseline_graph=_baseline_graph(instrument_id),
    )

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise _ForcedMergeFailureError("forced failure partway through the build step")

    # Fault-injection seam proving the all-or-nothing guarantee against real, live state
    # detroit-exception: fault seam proving the all-or-nothing guarantee
    monkeypatch.setattr(merge_primitives_module, "build_canonical_node_primitives", _raise)

    try:
        with live_restore(provisioned_graph_log, live_falkordb) as live:
            with pytest.raises(_ForcedMergeFailureError):
                restore_instrument(
                    artifact,
                    db=live_falkordb,
                    single_tenant_graph_name=single_tenant_graph_name,
                    similarity_threshold=_SIMILARITY_THRESHOLD,
                    actor=_ACTOR,
                    open_gateway=live.open_gateway,
                    audit_event_id=live.audit_event_id,
                    emitter=emitter,
                )

            # No log entries exist under the restore's audit event.
            assert live.gateway.read_log_position(single_tenant_graph_name) == 0
            assert live.gateway.read_log_position(native_target) == 0
            assert live.gateway.read_log_position(baseline_target) == 0

        # The live single-tenant graph is byte-for-byte unchanged (compared as SerializedGraphs).
        after = serialize_graph(graph_query_handle(live_falkordb, single_tenant_graph_name))
        assert after == before

        # "Neither graph": the native and baseline legs were never written either.
        assert live_falkordb.connection.exists(native_target) == 0
        assert live_falkordb.connection.exists(baseline_target) == 0
    finally:
        live_falkordb.connection.delete(native_target, baseline_target, single_tenant_graph_name)
