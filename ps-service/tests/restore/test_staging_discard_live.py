"""Live FalkorDB proof for `ps_service.restore.staging.discard_staged_keys` (PLAN.md Slice 2.8)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from ps_service.export.falkordb_connection import raw_connection
from ps_service.restore.staging import discard_staged_keys

if TYPE_CHECKING:
    from falkordb import FalkorDB


@pytest.mark.falkordb_live
def test_discard_staged_keys_deletes_existing_and_ignores_missing(live_falkordb: FalkorDB) -> None:
    suffix = uuid.uuid4().hex
    existing = f"__ac66_slice28_existing_{suffix}__"
    missing = f"__ac66_slice28_missing_{suffix}__"
    live_falkordb.select_graph(existing).query("CREATE (:Test {id: 'x'})")
    assert live_falkordb.connection.exists(missing) == 0
    connection = raw_connection(live_falkordb)

    discard_staged_keys(connection, (existing, missing))

    assert live_falkordb.connection.exists(existing) == 0
    assert live_falkordb.connection.exists(missing) == 0
