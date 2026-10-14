"""Live proof that an interruption before the group is submitted leaves nothing behind.

The REAL `restore_instrument` runs against the real gateway (Postgres graph log, FalkorDB) with
a gateway whose multi-graph submit is interrupted, simulating a hard process stop after every leg
was built and before the log transaction. Asserts the live `{short}_native`/`{short}_baseline` and
single-tenant graphs are unchanged, the log holds nothing for them, and no extra graph key exists
at all (a restore stages nothing in FalkorDB). The post-commit interruption matrix is in
`test_restore_swap.py`.

Not run in the implementation sandbox (no FalkorDB, no Postgres).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.export.models import SerializedGraph, SerializedNode
from ps_service.export.serialize import serialize_graph
from ps_service.restore.restore_instrument import restore_instrument
from restore._fixtures import build_restore_artifact
from restore.live_gateway import live_restore

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB
    from persistence.provisioned_postgres import Provisioned

_SIMILARITY_THRESHOLD = 0.9


class _InterruptedError(Exception):
    """The simulated hard stop before the log transaction."""


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
@pytest.mark.postgres_live
def test_interruption_before_the_submit_leaves_every_live_target_untouched(
    live_falkordb: FalkorDB,
    provisioned_graph_log: Provisioned,
    make_emitter: MakeEmitter,
) -> None:
    emitter, _log_path = make_emitter()
    token = uuid.uuid4().hex[:12]
    short_name = f"RT58{token}"
    single_tenant_graph_name = f"__ac66_slice58_single_tenant_{token}__"
    native_target = f"{short_name.lower()}_native"
    baseline_target = f"{short_name.lower()}_baseline"
    instrument_id = f"RT58-{token}"

    live_falkordb.select_graph(single_tenant_graph_name).query(
        "CREATE (:Capability {id: 'cap_pre_existing_5_8', name: 'Pre-existing'})"
    )
    before = serialize_graph(graph_query_handle(live_falkordb, single_tenant_graph_name))
    artifact = build_restore_artifact(
        instrument_id=instrument_id,
        short_name=short_name,
        native_graph=_native_graph(instrument_id),
        baseline_graph=_baseline_graph(instrument_id),
    )
    keys_before = set(live_falkordb.list_graphs())

    try:
        with live_restore(provisioned_graph_log, live_falkordb) as live:

            class _InterruptedGateway:
                read_log_position = live.gateway.read_log_position
                is_caught_up = live.gateway.is_caught_up
                catch_up = live.gateway.catch_up
                submit_group = live.gateway.submit_group

                def submit_multi_graph_group(self, group: object) -> object:
                    del group
                    raise _InterruptedError

            interrupted = _InterruptedGateway()
            with pytest.raises(_InterruptedError):
                restore_instrument(
                    artifact,
                    db=live_falkordb,
                    single_tenant_graph_name=single_tenant_graph_name,
                    similarity_threshold=_SIMILARITY_THRESHOLD,
                    actor="test-actor",
                    open_gateway=lambda: interrupted,
                    audit_event_id=live.audit_event_id,
                    emitter=emitter,
                )

            assert live.gateway.read_log_position(native_target) == 0
            assert live.gateway.read_log_position(baseline_target) == 0

        assert live_falkordb.connection.exists(native_target) == 0
        assert live_falkordb.connection.exists(baseline_target) == 0
        after = serialize_graph(graph_query_handle(live_falkordb, single_tenant_graph_name))
        assert after == before
        # Nothing was staged: no new graph key of any kind exists.
        assert set(live_falkordb.list_graphs()) == keys_before
    finally:
        live_falkordb.connection.delete(native_target, baseline_target, single_tenant_graph_name)
