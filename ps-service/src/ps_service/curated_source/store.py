"""FalkorDB persistence for the curated-content source runtime override (issue #125, Slice 3).

A singleton `CatalogSourceOverride {id: "singleton"}` node in the same
`policy_system` graph `ps_service.company_merge.pending_review` already
writes into (D-PERSISTENCE) -- one node, `MERGE`d/read/`DELETE`d by plain
parameterized Cypher, mirroring `pending_review.py`'s own CRUD/`_execute_query`
connectivity-wrapping shape exactly (own copy, not a shared import -- that
module's own documented "deliberate near-duplicate" convention,
`company_merge/falkordb_client.py`'s module docstring).

`get_override` is a plain, unwrapped read (mirrors `pending_review.
list_pending_reviews`'s own "every query here is read-only, no
dependency-health wrapper" convention) -- any failure (FalkorDB unreachable,
a `redis.exceptions.RedisError`, or anything else) propagates to the caller
unchanged. `ps_service.curated_source.resolve.resolve_effective_source` is
the one caller that relies on this: it treats ANY exception from this read
(or from opening the graph in the first place) as "no override" (D-FAILOPEN).

`set_override`/`reset_override` are writes, routed through this module's own
`_execute_query` -- a `redis.exceptions.RedisError` there is translated to
`CuratedSourceOverridePersistenceError` (this component's own persistence
failure type, mirroring `CompanyMergePersistenceError`) and recorded on
`ps_service.dependency_health.FALKORDB`, exactly like `pending_review.py`'s
own write path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, cast

import redis.exceptions

from ps_service.curated_source.errors import CuratedSourceOverridePersistenceError
from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy
from ps_service.logging.facade import emit_log_entry

if TYPE_CHECKING:
    from ps_service.logging import LogEmitter

__all__ = ["GraphHandle", "GraphQueryResult", "get_override", "reset_override", "set_override"]

_SINGLETON_ID = "singleton"

_GET_OVERRIDE_QUERY = "MATCH (o:CatalogSourceOverride {id: $id}) RETURN o.url"
_SET_OVERRIDE_QUERY = (
    "MERGE (o:CatalogSourceOverride {id: $id}) SET o.url = $url, o.updated_at = $updated_at"
)
_RESET_OVERRIDE_QUERY = "MATCH (o:CatalogSourceOverride {id: $id}) DELETE o"

_COMPONENT = "curated_source"
_SET_ACTION = "set_catalog_source_override"
_RESET_ACTION = "reset_catalog_source_override"


class GraphQueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult` -- own copy, mirrors `pending_review.py`."""

    @property
    def result_set(self) -> list[object]:
        """The query's result rows, one list of column values per row."""
        ...


class GraphHandle(Protocol):
    """Structural stand-in for `falkordb.Graph` -- own copy, mirrors `pending_review.py`."""

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        """Run Cypher `q` (optionally parameterized via `params`) and return the result."""
        ...


def _execute_query(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> GraphQueryResult:
    """Wrap every `graph.query()` write in this module for connectivity-health recording.

    Own copy of `pending_review._execute_query`'s exact shape (D-PERSISTENCE)
    -- a fresh private copy, not a shared import of another module's
    private, underscore-prefixed function.
    """
    try:
        result = graph.query(query, params=params)
    except redis.exceptions.RedisError as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise CuratedSourceOverridePersistenceError(f"FalkorDB write failed: {exc}") from exc
    mark_healthy(FALKORDB)
    return result


def get_override(graph: GraphHandle) -> str | None:
    """Return the persisted curated-content source override URL, or `None` if unset.

    A plain, unwrapped read (see module docstring) -- any exception
    (including a FalkorDB connectivity failure) propagates unchanged to the
    caller, which is exactly what `resolve.resolve_effective_source`'s
    D-FAILOPEN fallback relies on.
    """
    result = graph.query(_GET_OVERRIDE_QUERY, params={"id": _SINGLETON_ID})
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return None
    return cast("str", rows[0][0])


def set_override(graph: GraphHandle, url: str, *, emitter: LogEmitter | None = None) -> None:
    """Persist `url` as the effective curated-content source override (AC-BI-012).

    `url` is trusted to already be http(s)/TLS-validated
    (`source_url.validate_source_url` runs before this is ever reached, from
    the `set-catalog-source` MCP tool) -- this function performs the write
    only; it does not re-validate the scheme.

    Raises:
        CuratedSourceOverridePersistenceError: The FalkorDB write failed.
    """
    _execute_query(
        graph,
        _SET_OVERRIDE_QUERY,
        params={"id": _SINGLETON_ID, "url": url, "updated_at": datetime.now(UTC).isoformat()},
    )
    emit_log_entry(
        component=_COMPONENT,
        action=_SET_ACTION,
        outcome="success",
        extra={"url": url},
        emitter=emitter,
    )


def reset_override(graph: GraphHandle, *, emitter: LogEmitter | None = None) -> None:
    """Delete the persisted curated-content source override, if any (AC-BI-014).

    A no-op (still emits the success log entry) when no override was
    persisted -- `DELETE` on a `MATCH` that finds nothing simply matches zero
    rows, mirroring `pending_review`'s own `DELETE r` idiom.

    Raises:
        CuratedSourceOverridePersistenceError: The FalkorDB write failed.
    """
    _execute_query(graph, _RESET_OVERRIDE_QUERY, params={"id": _SINGLETON_ID})
    emit_log_entry(component=_COMPONENT, action=_RESET_ACTION, outcome="success", emitter=emitter)
