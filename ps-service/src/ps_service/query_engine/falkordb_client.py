"""FalkorDB connection surface for `ps_service.query_engine`.

Own copy of `ps_service.company_merge.falkordb_client`'s/`ps_service.
domain_mapper.falkordb_client`'s shape (PLAN_REVIEWED.md §1/§2.3 -- a
deliberate near-duplicate, not a shared import): `GraphHandle`/
`GraphQueryResult` Protocols and `connect`/`connect_from_config`/
`select_graph`.

The one real difference from those two narrower vendored copies:
`GraphQueryResult` here also exposes `header`, because
`execute_cypher_query`'s success path (a later increment) derives column
NAMES from `result.header` (`[type, name]` pairs), not just `result_set`.

Deliberately NOT included here, unlike the company_merge/domain_mapper
copies: `check_connectivity`/`dependency_health` wiring. No AC requires a
startup connectivity probe for Query Engine; the original `cypher_cli.py`'s
`_connect` does not probe connectivity either (lazy connection, first
`.query()` call) -- this preserves that behavior exactly (PLAN_REVIEWED.md
§2.3).
"""

from __future__ import annotations

from typing import Protocol

from falkordb import (  # pyright: ignore[reportMissingTypeStubs] -- falkordb ships no py.typed marker; this is the one boundary import
    FalkorDB,
)

from ps_service.config import ServiceConfig


class GraphQueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult`. Includes `header`
    because `execute_cypher_query`'s success path must derive column names
    from `result.header` (`[type, name]` pairs), not just `result_set`."""

    @property
    def header(self) -> list[list[object]]: ...

    @property
    def result_set(self) -> list[object]: ...


class GraphHandle(Protocol):
    """Structural stand-in for `falkordb.Graph` -- the `query()` call
    surface `execute_cypher_query` needs. Callers outside this module never
    import `falkordb.Graph` directly."""

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult: ...


def connect(host: str, port: int) -> FalkorDB:
    """Construct a FalkorDB client. Does not itself verify connectivity --
    the underlying client connects lazily on first use, mirroring the
    original `cypher_cli.py`'s `_connect` behavior exactly."""
    return FalkorDB(host=host, port=port)


def connect_from_config(config: ServiceConfig) -> FalkorDB:
    """Build the real FalkorDB connection from `config.falkordb_host`/
    `config.falkordb_port` -- mirrors `ps_service.company_merge.
    falkordb_client.connect_from_config`/`ps_service.domain_mapper.
    falkordb_client.connect_from_config` exactly. Callers build `config` via
    `ps_service.config.load_config()`, never by constructing a
    `ServiceConfig` with hardcoded host/port values.
    """
    return connect(host=config.falkordb_host, port=config.falkordb_port)


def select_graph(db: FalkorDB, name: str) -> GraphHandle:
    """The single conversion site from a real `falkordb.Graph` to this
    module's local `GraphHandle` Protocol -- callers elsewhere in
    `ps_service.query_engine` never import `falkordb.Graph` directly."""
    return db.select_graph(name)
