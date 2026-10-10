"""The digest scans read properties only for the rows they return (#207 S19).

`WITH n, properties(n) AS p ORDER BY id(n) LIMIT k` makes FalkorDB build the property map of every
node after the cursor before it sorts and cuts the chunk, so a scan of N nodes cost O(N^2) map
builds (with embeddings, the dominant cost of a CRA x10 digest: 422 ms for the first chunk of 50
against 154 ms, and the gap grows with the graph). The chunk is cut first, properties are read
after.
"""

from __future__ import annotations

import pytest

from ps_service.graph_gateway.cypher import DIGEST_EDGE_SCAN, DIGEST_NODE_SCAN


@pytest.mark.parametrize("scan", [DIGEST_NODE_SCAN, DIGEST_EDGE_SCAN], ids=["nodes", "edges"])
def test_the_chunk_is_cut_before_any_property_map_is_built(scan: str) -> None:
    assert scan.index("LIMIT $limit") < scan.index("properties(")
