"""Live proof of the restore's optimistic concurrency on `policy_system`.

The REAL `restore_instrument` runs against the real gateway (Postgres graph log, FalkorDB). The
guard is the log position the `policy_system` leg is built at (`ExpectedPosition`), so the race is a
second WRITER going through the gateway between the restore's position read and its submit. The
two scenarios:

(a) A rival group lands once, in that window -- the first set is refused (nothing logged on any
    graph), the restore reads again and completes carrying BOTH the rival's node and its own
    merged Capability (the additive, never-clobber invariant).
(b) A rival group lands in that window on EVERY attempt -- after `MAX_POLICY_SYSTEM_ATTEMPTS` the
    restore raises `RestoreConcurrencyConflictError`, no `{short}_native`/`{short}_baseline` graph
    was created and `policy_system` holds ONLY the pre-existing seed and the rival's own writes.

Not run in the implementation sandbox (no FalkorDB, no Postgres); the fast twin is
`test_restore_concurrency.py`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from company_merge._thresholds import REVIEW_THRESHOLD

from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.graph_gateway.models import MutationGroup, UpsertNode
from ps_service.restore.errors import RestoreConcurrencyConflictError
from ps_service.restore.graph_submission import MAX_POLICY_SYSTEM_ATTEMPTS
from ps_service.restore.restore_instrument import restore_instrument
from restore._fixtures import build_restore_artifact
from restore.live_gateway import live_restore

if TYPE_CHECKING:
    from collections.abc import Callable

    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB
    from persistence.provisioned_postgres import Provisioned

    from ps_service.graph_gateway.gateway import GraphWriteGateway

_ACTOR = "test-actor"
_MERGE_THRESHOLD = 0.9
_CAPABILITY_ID = "cap_concurrency_proof"


class _RivalGateway:
    """The live gateway, except a rival group lands right after the restore's position reads.

    `rivals` is how many reads are followed by one (the first N). The rival writes through the
    same gateway, so it moves the log position, which is exactly what `ExpectedPosition` guards.
    """

    def __init__(
        self, inner: GraphWriteGateway, graph: str, audit_event_id: str, rivals: int
    ) -> None:
        self._inner = inner
        self._graph = graph
        self._audit_event_id = audit_event_id
        self._rivals = rivals
        self.reads = 0
        self.submit_multi_graph_group = inner.submit_multi_graph_group
        self.submit_group = inner.submit_group
        self.is_caught_up = inner.is_caught_up
        self.catch_up = inner.catch_up

    def read_log_position(self, graph: str) -> int:
        position = self._inner.read_log_position(graph)
        if graph == self._graph:
            self.reads += 1
            if self.reads <= self._rivals:
                self._inner.submit_group(
                    MutationGroup(
                        graph=self._graph,
                        audit_event_id=self._audit_event_id,
                        primitives=(
                            UpsertNode(
                                label="Capability",
                                id=f"concurrent-{self.reads}",
                                properties={"name": f"concurrent {self.reads}"},
                            ),
                        ),
                    )
                )
        return position


def _baseline_graph(instrument_id: str) -> SerializedGraph:
    role_id = "role_5_6b"
    requirement_id = f"{instrument_id}_req_art_1"
    obligation_id = "obligation_5_6b"
    return SerializedGraph(
        nodes=(
            SerializedNode(
                label="RegulatoryInstrument", properties={"id": instrument_id, "title": "RT56B"}
            ),
            SerializedNode(
                label="Role", properties={"id": role_id, "name": "Role 5.6b", "confidence": 0.9}
            ),
            SerializedNode(
                label="Requirement",
                properties={
                    "id": requirement_id,
                    "text": "Requirement 5.6b",
                    "type": "obligation",
                    "confidence": 0.9,
                    "role_id": role_id,
                },
            ),
            SerializedNode(
                label="Obligation",
                properties={"id": obligation_id, "text": "Obligation 5.6b", "confidence": 0.9},
            ),
            SerializedNode(
                label="Capability",
                properties={"id": _CAPABILITY_ID, "name": "Concurrency Proof", "confidence": 0.9},
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
                target_id=_CAPABILITY_ID,
                properties={},
            ),
        ),
    )


def _empty_native_graph() -> SerializedGraph:
    return SerializedGraph(nodes=(), edges=())


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_restore_instrument_retries_past_a_concurrent_write_and_keeps_both(
    live_falkordb: FalkorDB,
    provisioned_graph_log: Provisioned,
    make_emitter: MakeEmitter,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    emitter, _log_path = make_emitter()
    token = uuid.uuid4().hex[:12]
    short_name = f"RT56B{token}"
    single_tenant_graph_name = f"__ac66_slice56b_single_tenant_{token}__"
    # restore_instrument lowercases short_name to match ingestion/domain_mapper's
    # own {short_name.lower()}_native/_baseline convention -- mirrored here so
    # this test's expected target names match the real, fixed graph keys.
    native_target = f"{short_name.lower()}_native"
    baseline_target = f"{short_name.lower()}_baseline"
    instrument_id = f"RT56B-{token}"

    live_falkordb.select_graph(single_tenant_graph_name).query(
        "CREATE (:Capability {id: 'seed', name: 'Seed'})"
    )
    artifact = build_restore_artifact(
        instrument_id=instrument_id,
        short_name=short_name,
        native_graph=_empty_native_graph(),
        baseline_graph=_baseline_graph(instrument_id),
    )

    try:
        with live_restore(provisioned_graph_log, live_falkordb) as live:
            rival = _RivalGateway(
                live.gateway, single_tenant_graph_name, live.audit_event_id, rivals=1
            )
            restore_instrument(
                artifact,
                db=live_falkordb,
                single_tenant_graph_name=single_tenant_graph_name,
                merge_threshold=_MERGE_THRESHOLD,
                review_threshold=REVIEW_THRESHOLD,
                actor=_ACTOR,
                open_gateway=lambda: rival,
                audit_event_id=live.audit_event_id,
                emitter=emitter,
            )

        assert rival.reads == 2  # attempt 1 was refused (stale), attempt 2 took effect

        node_ids = query_result_rows(
            live_falkordb, single_tenant_graph_name, "MATCH (n) RETURN n.id ORDER BY n.id"
        )
        # Both the rival writer's node AND the restore's own merged content survive.
        assert ["concurrent-1"] in node_ids
        assert ["seed"] in node_ids
        assert [_CAPABILITY_ID] in node_ids
    finally:
        live_falkordb.connection.delete(native_target, baseline_target, single_tenant_graph_name)


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_restore_instrument_raises_after_exhausting_retries_leaving_only_concurrent_writes(
    live_falkordb: FalkorDB,
    provisioned_graph_log: Provisioned,
    make_emitter: MakeEmitter,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    emitter, _log_path = make_emitter()
    token = uuid.uuid4().hex[:12]
    short_name = f"RT56Bx{token}"
    single_tenant_graph_name = f"__ac66_slice56b_exhausted_{token}__"
    native_target = f"{short_name.lower()}_native"
    baseline_target = f"{short_name.lower()}_baseline"
    instrument_id = f"RT56Bx-{token}"

    live_falkordb.select_graph(single_tenant_graph_name).query(
        "CREATE (:Capability {id: 'seed', name: 'Seed'})"
    )
    artifact = build_restore_artifact(
        instrument_id=instrument_id,
        short_name=short_name,
        native_graph=_empty_native_graph(),
        baseline_graph=_baseline_graph(instrument_id),
    )

    try:
        with live_restore(provisioned_graph_log, live_falkordb) as live:
            rival = _RivalGateway(
                live.gateway,
                single_tenant_graph_name,
                live.audit_event_id,
                rivals=MAX_POLICY_SYSTEM_ATTEMPTS,
            )
            with pytest.raises(RestoreConcurrencyConflictError):
                restore_instrument(
                    artifact,
                    db=live_falkordb,
                    single_tenant_graph_name=single_tenant_graph_name,
                    merge_threshold=_MERGE_THRESHOLD,
                    review_threshold=REVIEW_THRESHOLD,
                    actor=_ACTOR,
                    open_gateway=lambda: rival,
                    audit_event_id=live.audit_event_id,
                    emitter=emitter,
                )

        assert rival.reads == MAX_POLICY_SYSTEM_ATTEMPTS  # then give up

        # All-or-nothing on exhausted retries: no {short}_native/{short}_baseline graph was ever
        # created, and policy_system holds ONLY the pre-existing seed and the rival's own writes.
        assert live_falkordb.connection.exists(native_target) == 0
        assert live_falkordb.connection.exists(baseline_target) == 0
        node_ids = {
            row[0]
            for row in query_result_rows(
                live_falkordb, single_tenant_graph_name, "MATCH (n) RETURN n.id"
            )
        }
        assert node_ids == {"seed", "concurrent-1", "concurrent-2", "concurrent-3"}
        assert _CAPABILITY_ID not in node_ids
    finally:
        live_falkordb.connection.delete(native_target, baseline_target, single_tenant_graph_name)
