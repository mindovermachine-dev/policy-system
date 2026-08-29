"""FalkorDB connection surface for `ps_service.domain_mapper`.

Own copy of `ps_service.ingestion.falkordb_client`'s shape (PLAN_REVIEWED.md
§1's "components stay independently readable/testable" design note — a
deliberate near-duplicate, not a shared import): `GraphHandle`/
`GraphQueryResult` Protocols, `connect`/`connect_from_config`/
`select_graph`, and this component's own graph-naming helpers.

Naming (PLAN_REVIEWED.md §8): `native_graph_name` matches Ingestion's own
`{short_name.lower()}_native` convention exactly — Domain Mapper reads the
same native graph Ingestion wrote, so the naming formula must be identical,
not just similarly-shaped. `baseline_graph_name` is this component's own
per-regulation write target (`{short_name.lower()}_baseline`, AC-005) —
never the company's merged single-tenant graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from falkordb import (  # pyright: ignore[reportMissingTypeStubs] -- falkordb ships no py.typed marker; this is the one boundary import
    FalkorDB,
)

from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy
from ps_service.domain_mapper.errors import DomainMapperConfigurationError

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig

_NATIVE_GRAPH_SUFFIX = "_native"
_BASELINE_GRAPH_SUFFIX = "_baseline"


class GraphQueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult`.

    The one field any caller of `GraphHandle.query` needs to read off a
    response.
    """

    @property
    def result_set(self) -> list[object]:
        """The rows returned by the query."""
        ...


class GraphHandle(Protocol):
    """Structural stand-in for `falkordb.Graph`.

    The `query()` call surface this component's adapters/graph
    writer/derivation logic need: bare Cypher, or Cypher + `params=` for
    parameterized reads/writes. Callers outside this module never import
    `falkordb.Graph` directly.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        """Run Cypher `q` (optionally parameterized via `params`) and return the result."""
        ...


class _ConnectivityProbe(Protocol):
    """Structural requirement for `check_connectivity`'s `db` parameter.

    Only the one call surface needed to verify connectivity. A real
    `connect()`-returned `FalkorDB` instance satisfies this structurally
    with no change; tests can substitute a lightweight fake instead of
    mocking the real client type.
    """

    def list_graphs(self) -> list[str]: ...


def connect(host: str, port: int) -> FalkorDB:
    """Construct a FalkorDB client.

    Does not itself verify connectivity — the underlying client connects
    lazily on first use; call `check_connectivity()` against the result to
    fail loud early.
    """
    return FalkorDB(host=host, port=port)


def connect_from_config(config: ServiceConfig) -> FalkorDB:
    """Build the real FalkorDB connection from `config`'s host/port.

    Mirrors `ps_service.ingestion.falkordb_client.connect_from_config`
    exactly. Callers build `config` via `ps_service.config.load_config()`,
    never by constructing a `ServiceConfig` with hardcoded host/port
    values.
    """
    return connect(host=config.falkordb_host, port=config.falkordb_port)


def check_connectivity(db: _ConnectivityProbe, host: str, port: int) -> None:
    """Fail loud, with a friendly message, before doing any real work.

    Raises `DomainMapperConfigurationError` (wrapping the underlying cause)
    if `db` cannot list its graphs — the cheapest real round-trip available
    to confirm the connection is actually usable. Records the outcome in
    `ps_service.dependency_health` either way, so a caller using this as a
    startup probe also feeds the live readiness signal.
    """
    try:
        db.list_graphs()
    except Exception as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise DomainMapperConfigurationError(
            f"FalkorDB connection failed at {host}:{port}. Is FalkorDB running? Error: {exc}"
        ) from exc
    mark_healthy(FALKORDB)


def select_graph(db: FalkorDB, name: str) -> GraphHandle:
    """Convert a real `falkordb.Graph` to this module's local `GraphHandle` Protocol.

    The single conversion site — callers elsewhere in
    `ps_service.domain_mapper` never import `falkordb.Graph` directly.

    `name` is expected to already be the final graph name (e.g. the result
    of `native_graph_name`/`baseline_graph_name`) — this function does no
    naming/namespacing of its own.
    """
    return db.select_graph(name)


def native_graph_name(short_name: str) -> str:
    """The same regulation-scoped native graph Ingestion wrote to.

    (`{short_name.lower()}_native`, e.g. `"CRA"` -> `"cra_native"`) —
    Domain Mapper reads this, never writes it.
    """
    return f"{short_name.lower()}{_NATIVE_GRAPH_SUFFIX}"


def baseline_graph_name(short_name: str) -> str:
    """PLAN_REVIEWED.md §8: one FalkorDB graph per regulation for this component's own writes.

    (`{short_name.lower()}_baseline`, e.g. `"CRA"` -> `"cra_baseline"`) —
    distinct from `{short_name}_native` and from the company's merged
    single-tenant graph. AC-005's isolation requirement.
    """
    return f"{short_name.lower()}{_BASELINE_GRAPH_SUFFIX}"
