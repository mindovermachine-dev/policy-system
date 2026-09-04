"""Live FalkorDB proof for AC-BI-008: a baseline-leg merge failure leaves the
live target untouched (PLAN.md Slice 5.7, critical -- "the single most
important proof in the whole plan" alongside Slice 5.6b; do not relax to a
fake-graph unit test).

Injects a fake `graph_writer.persist_canonical_nodes` that raises partway
through the merge step (D8 step 5). Asserts AFTER the call: the live
single-tenant graph is byte-for-byte unchanged from before the call
(compared as `SerializedGraph`s via `export.serialize.serialize_graph`, not
a raw byte comparison, since the artifact format is JSON now, not a DUMP
blob); `{short}_native` does NOT exist -- the native leg's staged key was
never finalized either, even though it staged successfully first (D8's
"neither graph" requirement, proven across legs, not just within one).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

import ps_service.company_merge.graph_writer as graph_writer_module
from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.export.serialize import serialize_graph
from ps_service.restore.restore_instrument import restore_instrument
from restore._fixtures import build_restore_artifact

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB

_ACTOR = "test-actor"
_SIMILARITY_THRESHOLD = 0.9


class _ForcedMergeFailureError(Exception):
    """The merge-step failure this test deliberately injects."""


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
def test_merge_step_failure_leaves_single_tenant_and_native_untouched(
    live_falkordb: FalkorDB, make_emitter: MakeEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitter, _log_path = make_emitter()
    token = uuid.uuid4().hex[:12]
    short_name = f"RT57{token}"
    single_tenant_graph_name = f"__ac66_slice57_single_tenant_{token}__"
    native_target = f"{short_name}_native"
    baseline_target = f"{short_name}_baseline"
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
        raise _ForcedMergeFailureError("forced failure partway through the merge step")

    monkeypatch.setattr(graph_writer_module, "persist_canonical_nodes", _raise)

    try:
        with pytest.raises(_ForcedMergeFailureError):
            restore_instrument(
                artifact,
                db=live_falkordb,
                single_tenant_graph_name=single_tenant_graph_name,
                similarity_threshold=_SIMILARITY_THRESHOLD,
                actor=_ACTOR,
                emitter=emitter,
            )

        # The live single-tenant graph is byte-for-byte unchanged (compared as
        # SerializedGraphs, since the artifact format is JSON now, not a raw
        # DUMP blob).
        after = serialize_graph(graph_query_handle(live_falkordb, single_tenant_graph_name))
        assert after == before

        # D8's "neither graph": the native leg's staged key was never
        # finalized either, even though it staged successfully first.
        assert live_falkordb.connection.exists(native_target) == 0
        assert live_falkordb.connection.exists(baseline_target) == 0

        # No orphaned staged keys left behind (all discarded on the raise).
        staged_keys = live_falkordb.connection.keys(  # pyright: ignore[reportUnknownMemberType] -- redis-py: `.keys()`'s own stub signature carries an Unknown `**kwargs`
            f"*{short_name}*__restoring__*"
        )
        assert staged_keys == []
        snapshot_keys = live_falkordb.connection.keys(  # pyright: ignore[reportUnknownMemberType]
            f"{single_tenant_graph_name}__restoring__*"
        )
        assert snapshot_keys == []
    finally:
        live_falkordb.connection.delete(native_target, baseline_target, single_tenant_graph_name)
