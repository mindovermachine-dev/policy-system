"""FalkorDB replies are parsed by hiredis's C parser (#207 S19).

A graph reply is a tree of small tagged values; redis-py's pure-Python parser spent 5 ms on one
3,072-double embedding node and hiredis 2.4 ms, which decides whether a digest of a graph with
thousands of embeddings fits the replay budget (AC-RD-008). The dependency is pinned in
`ps-service/pyproject.toml`; this test fails if it is ever dropped or not installed.
"""

from __future__ import annotations

import redis.connection
import redis.utils


def test_redis_py_uses_the_hiredis_c_parser_for_graph_replies() -> None:
    assert redis.utils.HIREDIS_AVAILABLE
    assert redis.connection.DefaultParser.__name__ == "_HiredisParser"
