"""Live FalkorDB proof for `ps_service.restore.staging.stage_and_finalize_policy_system_leg`.

PLAN.md Slice 2.7 (CHANGES.md B1's fix) -- the WATCH-guarded optimistic-
concurrency retry loop for the `policy_system` leg. The native/baseline
`StagedLegNames` graphs are constructed directly via Cypher `CREATE` here
(standing in for what a real `stage_dump` call would have produced) -- this
function's own contract only cares that those staged keys exist and get
renamed, never how they were populated, so this test isolates exactly the
WATCH/MULTI/EXEC retry mechanism under test without depending on
`stage_dump`'s own `RESTORE`-based construction path (see
IMPL_SLICE_2.4.md/IMPL_SLICE_2.5.md for why that path is currently
unreliable on this sandbox's FalkorDB build -- unrelated to this function's
own correctness).

A real concurrent writer is simulated from *inside* the injected
`run_offline_merge` callback -- called by `stage_and_finalize_policy_
system_leg` itself after `WATCH` but before `MULTI`/`EXEC`, so the write
lands deterministically in the race window, no timing/sleep guesswork
needed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from ps_service.export.falkordb_connection import raw_connection
from ps_service.restore.errors import RestoreConcurrencyConflictError
from ps_service.restore.staging import StagedLegNames, stage_and_finalize_policy_system_leg

if TYPE_CHECKING:
    from collections.abc import Callable

    from falkordb import FalkorDB


@dataclass(frozen=True, slots=True)
class _SeededLeg:
    """The names this test suite's `_seed_leg` helper produces, bundled for readability."""

    single_tenant_name: str
    native: StagedLegNames
    baseline: StagedLegNames

    def all_names(self) -> tuple[str, ...]:
        return (
            self.single_tenant_name,
            self.native.staged_name,
            self.native.target_name,
            self.baseline.staged_name,
            self.baseline.target_name,
        )


def _seed_leg(live_falkordb: FalkorDB, suffix: str) -> _SeededLeg:
    single_tenant_name = f"__ac66_slice27_leg_{suffix}__"
    native = StagedLegNames(
        staged_name=f"__ac66_slice27_leg_native_staged_{suffix}__",
        target_name=f"__ac66_slice27_leg_native_target_{suffix}__",
    )
    baseline = StagedLegNames(
        staged_name=f"__ac66_slice27_leg_baseline_staged_{suffix}__",
        target_name=f"__ac66_slice27_leg_baseline_target_{suffix}__",
    )
    live_falkordb.select_graph(single_tenant_name).query("CREATE (:Seed {id: 'seed'})")
    live_falkordb.select_graph(native.staged_name).query("CREATE (:Test {id: 'native'})")
    live_falkordb.select_graph(baseline.staged_name).query("CREATE (:Test {id: 'baseline'})")
    return _SeededLeg(single_tenant_name, native, baseline)


@pytest.mark.falkordb_live
def test_policy_system_leg_succeeds_on_first_attempt_with_no_conflict(
    live_falkordb: FalkorDB,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    suffix = uuid.uuid4().hex
    leg = _seed_leg(live_falkordb, suffix)
    connection = raw_connection(live_falkordb)
    calls: list[str] = []

    try:
        stage_and_finalize_policy_system_leg(
            live_falkordb,
            connection,
            leg.single_tenant_name,
            suffix,
            calls.append,
            leg.native,
            leg.baseline,
        )

        assert len(calls) == 1
        assert live_falkordb.connection.exists(leg.native.target_name) == 1
        assert live_falkordb.connection.exists(leg.baseline.target_name) == 1
        assert live_falkordb.connection.exists(leg.native.staged_name) == 0
        assert live_falkordb.connection.exists(leg.baseline.staged_name) == 0
        assert (
            live_falkordb.connection.exists(f"{leg.single_tenant_name}__restoring__{suffix}_1") == 0
        )
        final_count = query_result_rows(
            live_falkordb, leg.single_tenant_name, "MATCH (n) RETURN count(n)"
        )
        assert final_count == [[1]]
    finally:
        live_falkordb.connection.delete(*leg.all_names())


@pytest.mark.falkordb_live
def test_policy_system_leg_retries_after_watch_error_and_succeeds(
    live_falkordb: FalkorDB,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    suffix = uuid.uuid4().hex
    leg = _seed_leg(live_falkordb, suffix)
    connection = raw_connection(live_falkordb)
    calls: list[str] = []

    def run_offline_merge(snapshot_name: str) -> None:
        calls.append(snapshot_name)
        if len(calls) == 1:
            # A concurrent writer races attempt 1, landing after WATCH but before EXEC.
            live_falkordb.select_graph(leg.single_tenant_name).query(
                "CREATE (:Concurrent {id: 'writer'})"
            )

    try:
        stage_and_finalize_policy_system_leg(
            live_falkordb,
            connection,
            leg.single_tenant_name,
            suffix,
            run_offline_merge,
            leg.native,
            leg.baseline,
        )

        assert len(calls) == 2  # attempt 1 aborted on WatchError, attempt 2 succeeded
        assert live_falkordb.connection.exists(leg.native.target_name) == 1
        assert live_falkordb.connection.exists(leg.baseline.target_name) == 1
        assert live_falkordb.connection.exists(leg.native.staged_name) == 0
        assert live_falkordb.connection.exists(leg.baseline.staged_name) == 0
        assert (
            live_falkordb.connection.exists(f"{leg.single_tenant_name}__restoring__{suffix}_1") == 0
        )
        # attempt 2's snapshot was taken AFTER the concurrent write landed, so the
        # finalized graph carries both the original seed and the concurrent writer's
        # node -- no lost update (AC-BI-006's additive/never-clobber invariant).
        final_ids = query_result_rows(
            live_falkordb, leg.single_tenant_name, "MATCH (n) RETURN n.id ORDER BY n.id"
        )
        assert final_ids == [["seed"], ["writer"]]
    finally:
        live_falkordb.connection.delete(*leg.all_names())


@pytest.mark.falkordb_live
def test_policy_system_leg_gives_up_after_exhausting_retries(
    live_falkordb: FalkorDB,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    suffix = uuid.uuid4().hex
    leg = _seed_leg(live_falkordb, suffix)
    connection = raw_connection(live_falkordb)
    calls: list[str] = []

    def run_offline_merge(snapshot_name: str) -> None:
        calls.append(snapshot_name)
        # A concurrent writer races EVERY attempt -- never lets EXEC win.
        live_falkordb.select_graph(leg.single_tenant_name).query(
            "CREATE (:Concurrent {id: $id})", params={"id": str(len(calls))}
        )

    try:
        with pytest.raises(RestoreConcurrencyConflictError) as exc_info:
            stage_and_finalize_policy_system_leg(
                live_falkordb,
                connection,
                leg.single_tenant_name,
                suffix,
                run_offline_merge,
                leg.native,
                leg.baseline,
            )

        assert isinstance(exc_info.value.__cause__, redis.exceptions.WatchError)
        assert len(calls) == 3  # exactly _MAX_POLICY_SYSTEM_MERGE_ATTEMPTS attempts, then give up

        # all-or-nothing: native/baseline staged keys discarded, no targets ever created
        assert live_falkordb.connection.exists(leg.native.staged_name) == 0
        assert live_falkordb.connection.exists(leg.baseline.staged_name) == 0
        assert live_falkordb.connection.exists(leg.native.target_name) == 0
        assert live_falkordb.connection.exists(leg.baseline.target_name) == 0
        for attempt in range(1, 4):
            snapshot_name = f"{leg.single_tenant_name}__restoring__{suffix}_{attempt}"
            assert live_falkordb.connection.exists(snapshot_name) == 0
        # the live single_tenant graph was never renamed/replaced -- it still exists under
        # its original name, holding only the concurrent writer's own writes plus the
        # original seed (restore never touched it, since it never won a MULTI/EXEC)
        assert live_falkordb.connection.exists(leg.single_tenant_name) == 1
        final_ids = query_result_rows(
            live_falkordb, leg.single_tenant_name, "MATCH (n) RETURN n.id ORDER BY n.id"
        )
        assert final_ids == [["1"], ["2"], ["3"], ["seed"]]
    finally:
        live_falkordb.connection.delete(*leg.all_names())
