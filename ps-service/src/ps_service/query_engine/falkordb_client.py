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
separate startup connectivity probe for Query Engine. Note the `falkordb`
client constructor itself issues a Redis round-trip (sentinel/cluster
detection), so construction fails fast when the server is unreachable;
`connect()` adds no further explicit probe (PLAN_REVIEWED.md §2.3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from falkordb import (  # pyright: ignore[reportMissingTypeStubs] -- falkordb ships no py.typed marker; this is the one boundary import
    FalkorDB,
)

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig


class GraphQueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult`.

    Includes `header` because `execute_cypher_query`'s success path must
    derive column names from `result.header` (`[type, name]` pairs), not
    just `result_set`.
    """

    @property
    def header(self) -> list[list[object]]:
        """`[type, name]` pairs, one per result column."""
        ...

    @property
    def result_set(self) -> list[object]:
        """Result rows exactly as returned by `falkordb`."""
        ...


class GraphHandle(Protocol):
    """Structural stand-in for `falkordb.Graph`.

    Exposes just the `query()` call surface `execute_cypher_query` needs.
    Callers outside this module never import `falkordb.Graph` directly.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        """Run Cypher `q` with optional `params`; return the raw result."""
        ...


def connect(host: str, port: int) -> FalkorDB:
    """Construct a FalkorDB client for `host`/`port`.

    The `falkordb` constructor issues a Redis round-trip (sentinel/cluster
    detection), so it fails fast if the server is unreachable; this
    function adds no further explicit probe.
    """
    return FalkorDB(host=host, port=port)


def connect_from_config(config: ServiceConfig) -> FalkorDB:
    """Build a FalkorDB connection from `config.falkordb_host`/`config.falkordb_port`.

    Mirrors `ps_service.company_merge.falkordb_client.connect_from_config`
    and `ps_service.domain_mapper.falkordb_client.connect_from_config`
    exactly. Callers build `config` via `ps_service.config.load_config()`,
    never by constructing a `ServiceConfig` with hardcoded host/port values.
    """
    return connect(host=config.falkordb_host, port=config.falkordb_port)


def select_graph(db: FalkorDB, name: str) -> GraphHandle:
    """Convert a real `falkordb.Graph` into this module's `GraphHandle`.

    The single conversion site for that cast -- callers elsewhere in
    `ps_service.query_engine` never import `falkordb.Graph` directly.
    """
    return db.select_graph(name)
