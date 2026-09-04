"""Live FalkorDB proof for CHANGES.md B1's concurrency scenario, run through
the REAL `restore_instrument` orchestration (New Slice 5.6b -- alongside
Slice 5.7, "the single most important proof in the whole plan").

`tests/restore/test_staging_policy_system_leg_live.py` already proves the
underlying `staging.stage_and_finalize_policy_system_leg` WATCH/MULTI/EXEC
retry mechanism in isolation, with a bare `calls.append` spy standing in for
`run_offline_merge`. This file proves the SAME mechanism survives
integration with `restore_instrument`'s real offline dedup+merge callback
(`resolve_capability_convergence_offline` + the unmodified `graph_writer.
persist_*` functions) -- not a fake-graph unit test, and not merely a retry
of the lower-level proof: the merge callback under test here is the real
one `restore_instrument` builds, reading/writing real staged FalkorDB keys.

CHANGES.md B1's exact scenario: seed the single-tenant graph with one node.
Start a restore's baseline-leg merge; after the merge callback's first
invocation begins (the snapshot for attempt 1 has already been taken) but
before `stage_and_finalize_policy_system_leg` reaches `pipe.execute()`, a
SECOND, independent `FalkorDB` connection runs a plain `CREATE (:Test
{id:'concurrent'})` against the live single-tenant graph -- simulating a
concurrent live ingestion write. `restore_instrument._run_baseline_merge`
only ever runs as that function's `run_offline_merge` argument -- called
strictly after `WATCH` and the attempt's `GRAPH.COPY` snapshot, strictly
before `pipe.multi()`/`pipe.execute()` -- so wrapping it with a spy that
fires the concurrent write on its first call lands the race deterministically
in the exact window CHANGES.md B1 describes, no timing/sleep guesswork
needed. Two variants:

(a) The concurrent write fires only on the merge callback's first
    invocation -- attempt 1 aborts on `WatchError`, attempt 2's snapshot
    (taken AFTER the concurrent write landed) carries it, and restore
    completes successfully carrying BOTH the concurrent writer's node AND
    the restore's own merged Capability -- AC-BI-006's additive/never-
    clobber invariant, proven empirically.
(b) The concurrent write fires on EVERY invocation, never letting `EXEC`
    win -- `restore_instrument` raises `RestoreConcurrencyConflictError`
    after exhausting its retries, and the live single-tenant graph
    afterward holds ONLY the concurrent writer's own writes (plus the
    original seed) -- AC-BI-008's all-or-nothing holds even on exhausted
    retries, not only on a caught merge-step exception (Slice 5.7) or a
    simulated hard-kill (Slice 5.8).
"""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from falkordb import (  # pyright: ignore[reportMissingTypeStubs] -- falkordb ships no py.typed marker
    FalkorDB,
)

import ps_service.restore.restore_instrument as restore_instrument_module
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.restore.errors import RestoreConcurrencyConflictError
from ps_service.restore.restore_instrument import restore_instrument
from restore._fixtures import build_restore_artifact

if TYPE_CHECKING:
    from collections.abc import Callable

    from company_merge._fakes import MakeEmitter

    from ps_service.logging.emitter import LogEmitter

_ACTOR = "test-actor"
_SIMILARITY_THRESHOLD = 0.9
_CAPABILITY_ID = "cap_concurrency_proof"

_REAL_RUN_BASELINE_MERGE = restore_instrument_module._run_baseline_merge  # pyright: ignore[reportPrivateUsage]


def _call_real_run_baseline_merge(
    db: FalkorDB,
    baseline_staged_name: str,
    regulatory_instrument_id: str,
    incoming_embeddings: dict[str, tuple[float, ...]],
    similarity_threshold: float,
    snapshot_name: str,
    emitter: LogEmitter | None,
) -> None:
    """Forward to the real `_run_baseline_merge`, with its own exact signature.

    Exists purely so both spies below can call through with a precisely
    typed signature (matching `_run_baseline_merge`'s own) rather than an
    untyped `*args`/`**kwargs` forward, which `basedpyright` cannot verify.
    """
    _REAL_RUN_BASELINE_MERGE(
        db,
        baseline_staged_name,
        regulatory_instrument_id,
        incoming_embeddings,
        similarity_threshold,
        snapshot_name,
        emitter,
    )


def _second_connection() -> FalkorDB:
    """A genuinely independent `FalkorDB` connection, standing in for a concurrent writer."""
    host = os.environ.get("PS_FALKORDB_HOST", "127.0.0.1")
    port = int(os.environ.get("PS_FALKORDB_PORT", "6379"))
    return FalkorDB(host=host, port=port)


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
def test_restore_instrument_retries_past_a_concurrent_write_and_keeps_both(
    live_falkordb: FalkorDB,
    make_emitter: MakeEmitter,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitter, _log_path = make_emitter()
    token = uuid.uuid4().hex[:12]
    short_name = f"RT56B{token}"
    single_tenant_graph_name = f"__ac66_slice56b_single_tenant_{token}__"
    native_target = f"{short_name}_native"
    baseline_target = f"{short_name}_baseline"
    instrument_id = f"RT56B-{token}"

    live_falkordb.select_graph(single_tenant_graph_name).query("CREATE (:Seed {id: 'seed'})")

    artifact = build_restore_artifact(
        instrument_id=instrument_id,
        short_name=short_name,
        native_graph=_empty_native_graph(),
        baseline_graph=_baseline_graph(instrument_id),
    )

    merge_call_count = 0
    concurrent_writer = _second_connection()

    def spying_run_baseline_merge(
        db: FalkorDB,
        baseline_staged_name: str,
        regulatory_instrument_id: str,
        incoming_embeddings: dict[str, tuple[float, ...]],
        similarity_threshold: float,
        snapshot_name: str,
        merge_emitter: LogEmitter | None,
    ) -> None:
        nonlocal merge_call_count
        merge_call_count += 1
        if merge_call_count == 1:
            # Lands deterministically after WATCH + the attempt-1 snapshot
            # (this callback only ever runs strictly after both, and
            # strictly before pipe.multi()/pipe.execute()) but before
            # stage_and_finalize_policy_system_leg finalizes.
            concurrent_writer.select_graph(single_tenant_graph_name).query(
                "CREATE (:Test {id: 'concurrent'})"
            )
        _call_real_run_baseline_merge(
            db,
            baseline_staged_name,
            regulatory_instrument_id,
            incoming_embeddings,
            similarity_threshold,
            snapshot_name,
            merge_emitter,
        )

    monkeypatch.setattr(restore_instrument_module, "_run_baseline_merge", spying_run_baseline_merge)

    try:
        restore_instrument(
            artifact,
            db=live_falkordb,
            single_tenant_graph_name=single_tenant_graph_name,
            similarity_threshold=_SIMILARITY_THRESHOLD,
            actor=_ACTOR,
            emitter=emitter,
        )

        assert merge_call_count == 2  # attempt 1 aborted on WatchError, attempt 2 retried+succeeded

        node_ids = query_result_rows(
            live_falkordb, single_tenant_graph_name, "MATCH (n) RETURN n.id ORDER BY n.id"
        )
        # Both the concurrent writer's node AND the restore's own merged
        # content survive -- no lost update (AC-BI-006).
        assert ["concurrent"] in node_ids
        assert ["seed"] in node_ids
        assert [_CAPABILITY_ID] in node_ids
    finally:
        live_falkordb.connection.delete(native_target, baseline_target, single_tenant_graph_name)


@pytest.mark.falkordb_live
def test_restore_instrument_raises_after_exhausting_retries_leaving_only_concurrent_writes(
    live_falkordb: FalkorDB,
    make_emitter: MakeEmitter,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitter, _log_path = make_emitter()
    token = uuid.uuid4().hex[:12]
    short_name = f"RT56Bx{token}"
    single_tenant_graph_name = f"__ac66_slice56b_exhausted_{token}__"
    native_target = f"{short_name}_native"
    baseline_target = f"{short_name}_baseline"
    instrument_id = f"RT56Bx-{token}"

    live_falkordb.select_graph(single_tenant_graph_name).query("CREATE (:Seed {id: 'seed'})")

    artifact = build_restore_artifact(
        instrument_id=instrument_id,
        short_name=short_name,
        native_graph=_empty_native_graph(),
        baseline_graph=_baseline_graph(instrument_id),
    )

    merge_call_count = 0
    concurrent_writer = _second_connection()

    def spying_run_baseline_merge(
        db: FalkorDB,
        baseline_staged_name: str,
        regulatory_instrument_id: str,
        incoming_embeddings: dict[str, tuple[float, ...]],
        similarity_threshold: float,
        snapshot_name: str,
        merge_emitter: LogEmitter | None,
    ) -> None:
        nonlocal merge_call_count
        merge_call_count += 1
        # A concurrent writer races EVERY attempt -- never lets EXEC win.
        concurrent_writer.select_graph(single_tenant_graph_name).query(
            "CREATE (:Test {id: $id})", {"id": f"concurrent-{merge_call_count}"}
        )
        _call_real_run_baseline_merge(
            db,
            baseline_staged_name,
            regulatory_instrument_id,
            incoming_embeddings,
            similarity_threshold,
            snapshot_name,
            merge_emitter,
        )

    monkeypatch.setattr(restore_instrument_module, "_run_baseline_merge", spying_run_baseline_merge)

    try:
        with pytest.raises(RestoreConcurrencyConflictError) as exc_info:
            restore_instrument(
                artifact,
                db=live_falkordb,
                single_tenant_graph_name=single_tenant_graph_name,
                similarity_threshold=_SIMILARITY_THRESHOLD,
                actor=_ACTOR,
                emitter=emitter,
            )

        assert isinstance(exc_info.value.__cause__, redis.exceptions.WatchError)
        assert merge_call_count == 3  # _MAX_POLICY_SYSTEM_MERGE_ATTEMPTS, then give up

        # All-or-nothing on exhausted retries: no {short}_native/{short}_baseline
        # target was ever created, and the live single-tenant graph holds ONLY
        # the concurrent writer's own writes (plus the pre-existing seed) --
        # never any of the restore's own merged Capability/Role/Requirement content.
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
