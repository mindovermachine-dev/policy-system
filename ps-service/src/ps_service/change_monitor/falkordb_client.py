"""FalkorDB connection surface for `ps_service.change_monitor`.

Own copy of `ps_service.company_merge.falkordb_client`'s shape
(PLAN_REVIEWED.md §1.3 -- a deliberate near-duplicate, not a shared import;
4 precedents: ingestion / query_engine / company_merge / domain_mapper):
`GraphHandle`/`GraphQueryResult`/`_ConnectivityProbe` Protocols,
`connect`/`connect_from_config`/`check_connectivity`/`select_graph`, and
this component's own graph-naming helpers.

Naming (PLAN_REVIEWED.md §1.3): `single_tenant_graph_name()` mirrors
`ps_service.company_merge.falkordb_client`'s
`os.environ.get("PS_FALKORDB_GRAPH", "policy_system")` convention exactly --
`poll_for_amendments` reads its tracked set from that same merged
single-tenant graph. `native_graph_name` matches Ingestion's own
`{short_name.lower()}_native` formula exactly -- `trigger_reingestion`'s
own bookkeeping writes land in the same per-regulation native graph
Ingestion wrote, so the formula must be identical, not just similar.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol

from falkordb import (  # pyright: ignore[reportMissingTypeStubs] -- falkordb ships no py.typed marker; this is the one boundary import
    FalkorDB,
)

from ps_service.change_monitor.errors import ChangeMonitorConfigurationError
from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig

_DEFAULT_SINGLE_TENANT_GRAPH_NAME = "policy_system"
_NATIVE_GRAPH_SUFFIX = "_native"


class GraphQueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult`.

    The one field any caller of `GraphHandle.query` needs to read off a
    response.
    """

    @property
    def result_set(self) -> list[object]:
        """The query's result rows, one list of column values per row."""
        ...


class GraphHandle(Protocol):
    """Structural stand-in for `falkordb.Graph`.

    The `query()` call surface this component's graph reader / succession
    logic needs: bare Cypher, or Cypher + `params=` for parameterized
    reads/writes. Callers outside this module never import `falkordb.Graph`
    directly.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        """Run Cypher `q` (optionally parameterized via `params`) and return the result."""
        ...


class _ConnectivityProbe(Protocol):
    """Structural requirement for `check_connectivity`'s `db` parameter.

    Only the one call surface needed to verify connectivity. A real
    `connect()`-returned `FalkorDB` instance satisfies this structurally with
    no change; tests can substitute a lightweight fake instead of mocking the
    real client type.
    """

    def list_graphs(self) -> list[str]: ...


def connect(host: str, port: int) -> FalkorDB:
    """Construct a FalkorDB client.

    Does not itself verify connectivity -- the underlying client connects
    lazily on first use; call `check_connectivity()` against the result to
    fail loud early.
    """
    return FalkorDB(host=host, port=port)


def connect_from_config(config: ServiceConfig) -> FalkorDB:
    """Build the real FalkorDB connection from `config.falkordb_host`/`config.falkordb_port`.

    Mirrors `ps_service.company_merge.falkordb_client.connect_from_config`
    exactly. Callers build `config` via `ps_service.config.load_config()`,
    never by constructing a `ServiceConfig` with hardcoded host/port values.
    """
    return connect(host=config.falkordb_host, port=config.falkordb_port)


def check_connectivity(db: _ConnectivityProbe, host: str, port: int) -> None:
    """Fail loud, with a friendly message, before doing any real work.

    Raises `ChangeMonitorConfigurationError` (wrapping the underlying cause)
    if `db` cannot list its graphs -- the cheapest real round-trip available
    to confirm the connection is actually usable. Records the outcome in
    `ps_service.dependency_health` either way, so a caller using this as a
    startup probe also feeds the live readiness signal.
    """
    try:
        db.list_graphs()
    except Exception as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise ChangeMonitorConfigurationError(
            f"FalkorDB connection failed at {host}:{port}. Is FalkorDB running? Error: {exc}"
        ) from exc
    mark_healthy(FALKORDB)


def select_graph(db: FalkorDB, name: str) -> GraphHandle:
    """Convert a real `falkordb.Graph` to this module's local `GraphHandle` Protocol.

    This is the single conversion site -- callers elsewhere in
    `ps_service.change_monitor` never import `falkordb.Graph` directly.
    `name` is expected to already be the final graph name (e.g. the result
    of `single_tenant_graph_name()` or `native_graph_name(...)`) -- this
    function does no naming/namespacing of its own.
    """
    return db.select_graph(name)


def single_tenant_graph_name() -> str:
    """The company's merged single-tenant regulatory graph name.

    `PS_FALKORDB_GRAPH`, defaulting to `"policy_system"`. Mirrors
    `ps_service.company_merge.falkordb_client.single_tenant_graph_name`
    exactly -- `poll_for_amendments` enumerates its tracked instrument set
    from this graph.
    """
    return os.environ.get("PS_FALKORDB_GRAPH", _DEFAULT_SINGLE_TENANT_GRAPH_NAME)


def native_graph_name(short_name: str) -> str:
    """The regulation-scoped native graph Ingestion wrote to.

    `{short_name.lower()}_native` (e.g. `"CRA"` -> `"cra_native"`) --
    matches `ps_service.ingestion.falkordb_client.native_graph_name`'s
    formula exactly. `trigger_reingestion`'s own bookkeeping writes land
    here.
    """
    return f"{short_name.lower()}{_NATIVE_GRAPH_SUFFIX}"
