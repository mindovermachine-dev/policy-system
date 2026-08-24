# © 2026 Cartman ApS. All rights reserved.
"""FalkorDB connection helper for `ps_service.ingestion`.

Ports `spikes/cellar1/falkordb_client.py`'s `connect`/`check_connectivity`/
`select_graph`/`GraphHandle` Protocol, retyped and cleaned per L1/L2 coding
standards (see PLAN_REVIEWED.md §7 Increment 6):

- `check_connectivity`'s `db` parameter is typed against a narrow local
  `_ConnectivityProbe` Protocol (only the `list_graphs()` call surface it
  actually needs) rather than the concrete `falkordb.FalkorDB` class the
  spike used — this is an L1 Dependency Inversion / L2 "use Protocol for
  interfaces" improvement over the spike: a real `connect()`-returned
  `FalkorDB` instance satisfies it structurally with no change, and tests
  can substitute a lightweight fake without a `unittest.mock.Mock(spec=...)`
  cast through the real client type.
- `GraphHandle.query`'s `params` is typed `dict[str, object] | None`
  (never `Any`) per L2 Types Handling's "no implicit Any" rule — the
  spike's own `GraphHandle` used `dict[str, Any]`. `dict`, not the more
  permissive `Mapping`, exactly to match `falkordb.Graph.query`'s own
  real (invariant) parameter type — a `Mapping`-typed parameter here would
  make the real `falkordb.Graph` fail this Protocol structurally, since
  `Graph.query` only ever declares `Dict[str, object] | None`.
- No `cellar1_`-style namespace prefix / `namespaced()` helper is carried
  forward — that was the spike's own optional `--namespace` CLI flag for
  test-isolation, not part of this component's contract. This module adds
  `native_graph_name` instead, implementing PLAN_REVIEWED.md §4.1's
  graph-per-regulation naming (`{short_name.lower()}_native`).
- `connect_from_config` (Increment 12, S4 fix, PLAN_REVIEWED.md §6.4/§7)
  composes this module's own `connect()` with `ServiceConfig.falkordb_host`/
  `falkordb_port` — the load-bearing use of the config fields Increment 7
  added to `ps_service.config`. It is the config-wired connection path the
  live end-to-end test (Increment 13) uses instead of hardcoding
  `"127.0.0.1"`/`6379` literals.
"""

from __future__ import annotations

from typing import Protocol

from falkordb import (  # pyright: ignore[reportMissingTypeStubs] -- falkordb ships no py.typed marker; this is the one boundary import
    FalkorDB,
)

from ps_service.config import ServiceConfig
from ps_service.ingestion.errors import IngestionConfigurationError

_NATIVE_GRAPH_SUFFIX = "_native"


class FalkorDBConnectionError(IngestionConfigurationError):
    """FalkorDB is unreachable at `check_connectivity` time.

    Subclasses `IngestionConfigurationError` (`ps_service.ingestion.errors`,
    Increment 1) — that type's own docstring already documents "Ingestion's
    FalkorDB connection could not be established from the resolved
    ServiceConfig... raised by falkordb_client.connect/connect_from_config"
    as its purpose. This is the concrete type actually raised, kept local to
    this module (matching the spike's own precedent of colocating the
    exception beside `connect()`/`check_connectivity()`) while remaining
    catchable as an `IngestionConfigurationError` by any caller relying on
    that broader family.
    """


class GraphQueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult` — the one field any
    caller of `GraphHandle.query` needs to read off a response."""

    @property
    def result_set(self) -> list[object]: ...


class GraphHandle(Protocol):
    """Structural stand-in for `falkordb.Graph` — the `query()` call
    surface `ingestion/graph_writer.py` (Increments 8-10) needs: bare
    Cypher, or Cypher + `params=` for parameterized writes. Callers outside
    this module never import `falkordb.Graph` directly; `select_graph`
    below is the one conversion site."""

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult: ...


class _ConnectivityProbe(Protocol):
    """Structural requirement for `check_connectivity`'s `db` parameter —
    only the one call surface needed to verify connectivity. A real
    `connect()`-returned `FalkorDB` instance satisfies this structurally
    with no change; tests can substitute a lightweight fake instead of
    mocking the real client type."""

    def list_graphs(self) -> list[str]: ...


def connect(host: str, port: int) -> FalkorDB:
    """Construct a FalkorDB client. Does not itself verify connectivity —
    the underlying client connects lazily on first use; call
    `check_connectivity()` against the result to fail loud early."""
    return FalkorDB(host=host, port=port)


def connect_from_config(config: ServiceConfig) -> FalkorDB:
    """Build the real FalkorDB connection from `config.falkordb_host`/
    `config.falkordb_port` (Increment 12, S4 fix — PLAN_REVIEWED.md §6.4).

    Composes this module's own `connect()` with the `ServiceConfig` fields
    Increment 7 added — the only call path in this component that is meant
    to build a live connection from resolved configuration rather than
    caller-supplied literals. Callers build `config` via
    `ps_service.config.load_config()`, never by constructing a
    `ServiceConfig` with hardcoded host/port values, so this function is
    what makes `PS_FALKORDB_HOST`/`PS_FALKORDB_PORT` genuinely load-bearing.
    """
    return connect(host=config.falkordb_host, port=config.falkordb_port)


def check_connectivity(db: _ConnectivityProbe, host: str, port: int) -> None:
    """Fail loud, with a friendly message, before doing any real work.

    Raises `FalkorDBConnectionError` (wrapping the underlying cause) if
    `db` cannot list its graphs — the cheapest real round-trip available to
    confirm the connection is actually usable.
    """
    try:
        db.list_graphs()
    except Exception as exc:
        raise FalkorDBConnectionError(
            f"FalkorDB connection failed at {host}:{port}. "
            f"Is FalkorDB running? Error: {exc}"
        ) from exc


def select_graph(db: FalkorDB, name: str) -> GraphHandle:
    """The single conversion site from a real `falkordb.Graph` to this
    module's local `GraphHandle` Protocol — callers elsewhere in
    `ps_service.ingestion` never import `falkordb.Graph` directly.

    `name` is expected to already be the final graph name (e.g. the result
    of `native_graph_name`) — this function does no naming/namespacing of
    its own.
    """
    return db.select_graph(name)


def native_graph_name(short_name: str) -> str:
    """PLAN_REVIEWED.md §4.1: one FalkorDB graph per regulation,
    `{short_name.lower()}_native` (e.g. `"CRA"` -> `"cra_native"`,
    `"GDPR"` -> `"gdpr_native"`). No `cellar1_`-style prefix — that was the
    spike's own test-isolation namespacing, not part of this component's
    contract.
    """
    return f"{short_name.lower()}{_NATIVE_GRAPH_SUFFIX}"
