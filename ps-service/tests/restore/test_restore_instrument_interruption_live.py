"""Live FalkorDB proof for AC-BI-008: an interruption between staging and
finalize leaves the live target untouched (PLAN.md Slice 5.8).

Runs D8's staging + merge steps directly (native/baseline staging via
`staging.stage_graph`, the merge via `restore_instrument._run_baseline_merge`
against a real `GRAPH.COPY` snapshot) -- stopping BEFORE
`staging.stage_and_finalize_policy_system_leg`'s own finalize
(`pipe.multi()`/`pipe.rename(...)`/`pipe.execute()`) ever runs, simulating a
hard process interruption without needing to actually kill a process.
Asserts the live `{short}_native`/`{short}_baseline`/single-tenant graphs are
unchanged -- only orphaned `__restoring__` staged keys exist, exactly as D8
step 6's own documented "abandoned keys are inert orphans" note describes.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

import ps_service.restore.restore_instrument as restore_instrument_module
from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.export.models import SerializedGraph, SerializedNode
from ps_service.export.serialize import serialize_graph
from ps_service.restore import schema_allowlist
from ps_service.restore.staging import snapshot_single_tenant, stage_graph

if TYPE_CHECKING:
    from collections.abc import Callable

    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB

_SIMILARITY_THRESHOLD = 0.9


def _native_graph(instrument_id: str) -> SerializedGraph:
    return SerializedGraph(
        nodes=(
            SerializedNode(
                label="RegulatoryInstrument", properties={"id": instrument_id, "title": "RT58"}
            ),
        ),
        edges=(),
    )


def _baseline_graph(instrument_id: str) -> SerializedGraph:
    return SerializedGraph(
        nodes=(
            SerializedNode(
                label="RegulatoryInstrument", properties={"id": instrument_id, "title": "RT58"}
            ),
            SerializedNode(
                label="Capability",
                properties={"id": "cap_5_8_new", "name": "New Capability", "confidence": 0.9},
            ),
        ),
        edges=(),
    )


@pytest.mark.falkordb_live
def test_interruption_before_finalize_leaves_every_live_target_untouched(
    live_falkordb: FalkorDB,
    make_emitter: MakeEmitter,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    emitter, _log_path = make_emitter()
    token = uuid.uuid4().hex[:12]
    short_name = f"RT58{token}"
    single_tenant_graph_name = f"__ac66_slice58_single_tenant_{token}__"
    native_target = f"{short_name}_native"
    baseline_target = f"{short_name}_baseline"
    instrument_id = f"RT58-{token}"

    live_falkordb.select_graph(single_tenant_graph_name).query(
        "CREATE (:Capability {id: 'cap_pre_existing_5_8', name: 'Pre-existing'})"
    )
    before = serialize_graph(graph_query_handle(live_falkordb, single_tenant_graph_name))

    native_graph = _native_graph(instrument_id)
    baseline_graph = _baseline_graph(instrument_id)
    native_staged_name = ""
    baseline_staged_name = ""
    snapshot_name = ""

    try:
        # D8 steps 2/3: stage native/baseline -- pure new-key writes.
        native_staged_name = stage_graph(
            live_falkordb,
            native_graph,
            short_name + "_native",
            allowed_labels=schema_allowlist.NATIVE_ALLOWED_LABELS,
            allowed_relationship_types=schema_allowlist.NATIVE_ALLOWED_RELATIONSHIP_TYPES,
        )
        baseline_staged_name = stage_graph(
            live_falkordb,
            baseline_graph,
            short_name + "_baseline",
            allowed_labels=schema_allowlist.BASELINE_ALLOWED_LABELS,
            allowed_relationship_types=schema_allowlist.BASELINE_ALLOWED_RELATIONSHIP_TYPES,
        )

        # D8 step 4: GRAPH.COPY snapshot of the live single-tenant graph.
        snapshot_name = f"{single_tenant_graph_name}__restoring__{token}"
        snapshot_single_tenant(live_falkordb, single_tenant_graph_name, snapshot_name)

        # D8 step 5: run the real offline merge into the snapshot only.
        incoming_embeddings = restore_instrument_module._capability_embeddings(  # pyright: ignore[reportPrivateUsage]
            baseline_graph
        )
        restore_instrument_module._run_baseline_merge(  # pyright: ignore[reportPrivateUsage]
            live_falkordb,
            baseline_staged_name,
            instrument_id,
            incoming_embeddings,
            _SIMILARITY_THRESHOLD,
            snapshot_name,
            emitter,
        )

        # STOP HERE -- simulating a hard interruption before
        # stage_and_finalize_policy_system_leg's own finalize
        # (pipe.multi()/pipe.rename(...)/pipe.execute()) ever runs.

        assert live_falkordb.connection.exists(native_target) == 0
        assert live_falkordb.connection.exists(baseline_target) == 0
        after = serialize_graph(graph_query_handle(live_falkordb, single_tenant_graph_name))
        assert after == before

        # The staged/snapshot keys exist as orphans (D8 step 6's own
        # documented "abandoned __restoring__ keys are inert orphans" note)
        # -- proving the interruption really did leave real, unfinalized
        # staged content sitting there, not that nothing ever ran.
        assert live_falkordb.connection.exists(native_staged_name) == 1
        assert live_falkordb.connection.exists(baseline_staged_name) == 1
        assert live_falkordb.connection.exists(snapshot_name) == 1
        snapshot_capability_rows = query_result_rows(
            live_falkordb, snapshot_name, "MATCH (c:Capability) RETURN c.id"
        )
        snapshot_capability_ids = {row[0] for row in snapshot_capability_rows}
        assert snapshot_capability_ids == {"cap_pre_existing_5_8", "cap_5_8_new"}
    finally:
        live_falkordb.connection.delete(
            native_target,
            baseline_target,
            single_tenant_graph_name,
            native_staged_name,
            baseline_staged_name,
            snapshot_name,
        )
