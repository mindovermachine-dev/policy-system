"""Live FalkorDB proof for `ps_service.restore.staging.snapshot_single_tenant`.

PLAN.md Slice 2.6, with CHANGES.md MA4's empty-key fix.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from ps_service.restore.staging import snapshot_single_tenant

if TYPE_CHECKING:
    from collections.abc import Callable

    from falkordb import FalkorDB

_COUNT_QUERY = "MATCH (n) RETURN count(n)"


@pytest.mark.falkordb_live
def test_snapshot_single_tenant_copies_current_content_and_is_independent_of_later_mutation(
    live_falkordb: FalkorDB,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    source_name = f"__ac66_slice26_source_{uuid.uuid4().hex}__"
    staged_name = f"__ac66_slice26_staged_{uuid.uuid4().hex}__"
    live_falkordb.select_graph(source_name).query("CREATE (:Test {id: 'a'})")

    try:
        result_name = snapshot_single_tenant(live_falkordb, source_name, staged_name)

        assert result_name == staged_name
        snapshot_count = query_result_rows(live_falkordb, staged_name, _COUNT_QUERY)
        assert snapshot_count == [[1]]

        # mutate the source AFTER snapshotting
        live_falkordb.select_graph(source_name).query("CREATE (:Test {id: 'b'})")

        source_count_after = query_result_rows(live_falkordb, source_name, _COUNT_QUERY)
        snapshot_count_after = query_result_rows(live_falkordb, staged_name, _COUNT_QUERY)
        assert source_count_after == [[2]]
        assert snapshot_count_after == [[1]]  # unaffected -- a true copy, not a reference
    finally:
        live_falkordb.connection.delete(source_name, staged_name)


@pytest.mark.falkordb_live
def test_snapshot_single_tenant_on_never_touched_key_creates_empty_staged_graph_live(
    live_falkordb: FalkorDB,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    never_touched_name = f"__ac66_slice26_fresh_{uuid.uuid4().hex}__"
    staged_name = f"__ac66_slice26_fresh_staged_{uuid.uuid4().hex}__"
    assert live_falkordb.connection.exists(never_touched_name) == 0

    try:
        result_name = snapshot_single_tenant(live_falkordb, never_touched_name, staged_name)

        assert result_name == staged_name
        count = query_result_rows(live_falkordb, staged_name, _COUNT_QUERY)
        assert count == [[0]]
        # MA4's fix vivifies only the STAGED name -- the never-touched source stays untouched
        assert live_falkordb.connection.exists(never_touched_name) == 0
    finally:
        live_falkordb.connection.delete(never_touched_name, staged_name)
