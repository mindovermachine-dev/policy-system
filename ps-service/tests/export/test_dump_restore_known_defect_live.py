"""Regression-documentation proof for a genuine FalkorDB server defect (see IMPL_SLICE_2.4.md).

This is **not** a test of `ps_service` code -- it exercises the raw `redis`/
`falkordb` primitives directly to pin down, precisely and reproducibly, an
external defect discovered while implementing PLAN.md Slice 2.4 (the
`dump_graph` + `RESTORE` round-trip proof): on this sandbox's FalkorDB
instance, `RESTORE`-ing a `DUMP`-produced blob into a *different* key
doubles the node count of BOTH the new key and the original source key the
blob was dumped from. Kept, deliberately not `xfail`, as a permanent,
executable record of the defect's exact shape -- if a future FalkorDB
image/version fixes this, this test's own assertions will start failing,
which is the intended signal to revisit `test_dump_roundtrip_live.py`'s
`xfail` marker and `restore.staging.stage_dump`'s live tests.

Investigation trail (all independently reproduced, not merely asserted
here): the raw `redis.Redis` client (bypassing `falkordb.FalkorDB`
entirely) reproduces it; using entirely separate connections for
create/dump/restore/verify reproduces it; `decode_responses=True` and
`decode_responses=False` both reproduce it; a 1-second delay between
`CREATE` and `DUMP` does not prevent it; pre-normalizing via `GRAPH.COPY`
before `DUMP` does not prevent it; a node-count sweep (1, 2, 3, 4, 5, 7, 8,
9, 16) shows an exact 2x in every case; an unrelated third graph never
touched by `DUMP`/`RESTORE` is unaffected (ruling out global/process-wide
corruption); `redis-cli MONITOR`-equivalent tracing (via `redis.Redis.
monitor()`) confirms `RESTORE` is sent to the server exactly once -- the
doubling is server-side, not a client retry. `GRAPH.COPY`/`RENAME` do not
exhibit any equivalent corruption (`tests/restore/test_staging_snapshot_
live.py`/`test_staging_finalize_live.py` pass cleanly).

CHANGES2.md note: this defect is why `ps_service.export`/`ps_service.restore`
no longer use `DUMP`/`RESTORE` at all (see `export/serialize.py`,
`restore/populate.py`) -- this test no longer gates any `ps_service`
behavior, but remains valuable, accurate, executable documentation of why
that redesign exists. See `test_serialize_roundtrip_live.py` for the
redesigned mechanism's own round-trip proof.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    from falkordb import FalkorDB

_COUNT_QUERY = "MATCH (n) RETURN count(n)"


@pytest.mark.falkordb_live
def test_restore_of_a_dump_blob_doubles_node_count_in_both_source_and_destination(
    live_falkordb: FalkorDB,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    source_name = f"__ac66_defect24_source_{uuid.uuid4().hex}__"
    restored_name = f"__ac66_defect24_restored_{uuid.uuid4().hex}__"
    live_falkordb.select_graph(source_name).query("CREATE (:Test {id: 1}), (:Test {id: 2})")

    try:
        before = query_result_rows(live_falkordb, source_name, _COUNT_QUERY)
        assert before == [[2]]

        blob = live_falkordb.connection.dump(source_name)
        assert blob is not None
        live_falkordb.connection.restore(restored_name, 0, blob)

        after_source = query_result_rows(live_falkordb, source_name, _COUNT_QUERY)
        after_restored = query_result_rows(live_falkordb, restored_name, _COUNT_QUERY)

        # The defect: both graphs now report DOUBLE the real node count (4, not 2).
        # If this assertion ever fails because the count is correctly [[2]], the
        # underlying FalkorDB defect has been fixed -- revisit test_dump_roundtrip_
        # live.py's xfail marker and restore.staging.stage_dump's live tests.
        assert after_source == [[4]]
        assert after_restored == [[4]]
    finally:
        live_falkordb.connection.delete(source_name, restored_name)


@pytest.mark.falkordb_live
def test_graph_copy_does_not_exhibit_the_same_corruption(
    live_falkordb: FalkorDB,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    """Control case: `GRAPH.COPY` (used by `snapshot_single_tenant`) is unaffected."""
    source_name = f"__ac66_defect24_copy_source_{uuid.uuid4().hex}__"
    copied_name = f"__ac66_defect24_copy_dest_{uuid.uuid4().hex}__"
    live_falkordb.select_graph(source_name).query("CREATE (:Test {id: 1}), (:Test {id: 2})")

    try:
        live_falkordb.select_graph(source_name).copy(copied_name)

        after_source = query_result_rows(live_falkordb, source_name, _COUNT_QUERY)
        after_copy = query_result_rows(live_falkordb, copied_name, _COUNT_QUERY)
        assert after_source == [[2]]
        assert after_copy == [[2]]
    finally:
        live_falkordb.connection.delete(source_name, copied_name)
