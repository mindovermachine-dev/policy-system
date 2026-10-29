"""`DUMP`/`RESTORE` of a graph key on the chart-pinned FalkorDB image keeps the graph intact.

These tests exercise the raw `redis`/`falkordb` primitives, not `ps_service` code. FalkorDB
graph-module builds up to at least 4.20.4 double the node count of both the source key and the
destination key when a `DUMP` blob is `RESTORE`d into a different key; the image pinned in
`charts/policy-system/values.yaml` (`falkordb/falkordb:6.0.3`) restores the graph unchanged.
The `DUMP`/`RESTORE` test passes on the pinned image and fails on an image with the doubling
defect, so moving the pin to such an image is caught here.

`ps_service.export` and `ps_service.restore` do not move graphs with `DUMP`/`RESTORE`: export
serializes a graph to canonical, checksummed JSON (`export/serialize.py`) and restore rebuilds it
through the graph gateway, which keeps a curated artifact readable and independent of the FalkorDB
on-disk encoding. `test_serialize_roundtrip_live.py` proves that round trip. `GRAPH.COPY`, which
the gateway's staged-write swap uses, is checked here alongside.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    from falkordb import FalkorDB

_NODE_COUNT_QUERY = "MATCH (n) RETURN count(n)"
_EDGE_COUNT_QUERY = "MATCH ()-[r]->() RETURN count(r)"
_SEED_QUERY = "CREATE (:Test {id: 1})-[:LINKS]->(:Test {id: 2})"


@pytest.mark.falkordb_live
def test_restore_of_a_dump_blob_into_another_key_preserves_source_and_destination(
    live_falkordb: FalkorDB,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    """Both keys hold exactly the seeded two nodes and one edge after `RESTORE`."""
    source_name = f"__dump_restore_source_{uuid.uuid4().hex}__"
    restored_name = f"__dump_restore_destination_{uuid.uuid4().hex}__"
    live_falkordb.select_graph(source_name).query(_SEED_QUERY)

    try:
        assert query_result_rows(live_falkordb, source_name, _NODE_COUNT_QUERY) == [[2]]

        blob = live_falkordb.connection.dump(source_name)
        assert blob is not None
        live_falkordb.connection.restore(restored_name, 0, blob)

        for name in (source_name, restored_name):
            assert query_result_rows(live_falkordb, name, _NODE_COUNT_QUERY) == [[2]], name
            assert query_result_rows(live_falkordb, name, _EDGE_COUNT_QUERY) == [[1]], name
    finally:
        live_falkordb.connection.delete(source_name, restored_name)


@pytest.mark.falkordb_live
def test_graph_copy_preserves_source_and_destination(
    live_falkordb: FalkorDB,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    """`GRAPH.COPY` leaves the source unchanged and gives the copy the same two nodes."""
    source_name = f"__graph_copy_source_{uuid.uuid4().hex}__"
    copied_name = f"__graph_copy_destination_{uuid.uuid4().hex}__"
    live_falkordb.select_graph(source_name).query(_SEED_QUERY)

    try:
        live_falkordb.select_graph(source_name).copy(copied_name)

        for name in (source_name, copied_name):
            assert query_result_rows(live_falkordb, name, _NODE_COUNT_QUERY) == [[2]], name
            assert query_result_rows(live_falkordb, name, _EDGE_COUNT_QUERY) == [[1]], name
    finally:
        live_falkordb.connection.delete(source_name, copied_name)
