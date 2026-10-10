"""Where the `falkordb_live` tests find their FalkorDB (issue #207).

`PS_TEST_FALKORDB_HOST` and `PS_TEST_FALKORDB_PORT` override the defaults, so a throwaway
container on a non-default port can stand in for a local FalkorDB on 127.0.0.1:6379.
"""

from __future__ import annotations

import os

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 6379


def falkordb_endpoint() -> tuple[str, int]:
    """Return the (host, port) of the FalkorDB the live tests use."""
    host = os.environ.get("PS_TEST_FALKORDB_HOST") or _DEFAULT_HOST
    port = int(os.environ.get("PS_TEST_FALKORDB_PORT") or _DEFAULT_PORT)
    return host, port
