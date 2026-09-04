"""Live FalkorDB proof for `ps_service.restore.staging.finalize_atomic_swap`.

PLAN.md Slice 2.7 -- the AC-BI-008 finalize-step proof for callers with no
concurrent writer (`{short}_native`/`{short}_baseline` legs).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from ps_service.export.falkordb_connection import raw_connection
from ps_service.restore.staging import finalize_atomic_swap

if TYPE_CHECKING:
    from collections.abc import Callable

    from falkordb import FalkorDB


@pytest.mark.falkordb_live
def test_finalize_atomic_swap_renames_all_staged_keys_into_place(live_falkordb: FalkorDB) -> None:
    suffix = uuid.uuid4().hex
    staged = [f"__ac66_slice27_staged_{i}_{suffix}__" for i in range(3)]
    targets = [f"__ac66_slice27_target_{i}_{suffix}__" for i in range(3)]
    for name in staged:
        live_falkordb.select_graph(name).query("CREATE (:Test {id: 'x'})")
    connection = raw_connection(live_falkordb)

    try:
        finalize_atomic_swap(connection, tuple(zip(staged, targets, strict=True)))

        for staged_name, target_name in zip(staged, targets, strict=True):
            assert live_falkordb.connection.exists(staged_name) == 0
            assert live_falkordb.connection.exists(target_name) == 1
    finally:
        live_falkordb.connection.delete(*staged, *targets)


@pytest.mark.falkordb_live
def test_finalize_atomic_swap_overwrites_preexisting_target_content(
    live_falkordb: FalkorDB,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    suffix = uuid.uuid4().hex
    staged_name = f"__ac66_slice27_staged_ow_{suffix}__"
    target_name = f"__ac66_slice27_target_ow_{suffix}__"
    live_falkordb.select_graph(staged_name).query("CREATE (:Test {id: 'new'})")
    live_falkordb.select_graph(target_name).query(
        "CREATE (:Test {id: 'old'}), (:Test {id: 'old2'})"
    )
    connection = raw_connection(live_falkordb)

    try:
        finalize_atomic_swap(connection, ((staged_name, target_name),))

        assert live_falkordb.connection.exists(staged_name) == 0
        result = query_result_rows(live_falkordb, target_name, "MATCH (n) RETURN n.id")
        assert result == [["new"]]
    finally:
        live_falkordb.connection.delete(staged_name, target_name)
