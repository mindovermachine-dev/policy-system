"""Read-only `{short}_baseline` graph reads for `ps_service.graph_cleanup`.

A baseline graph holds one instrument's Domain Mapper output as it was before Company Merge folded
it into the single-tenant graph, so it still holds a Capability that a semantic resolution never
minted. Every read here is a literal, parameterised query with no write keyword, run through
`graph_reader.query_rows` for the same health marking and generic persistence error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ps_service.graph_cleanup.graph_reader import query_rows
from ps_service.graph_cleanup.models import BaselineCapability

if TYPE_CHECKING:
    from collections.abc import Collection

    from ps_service.company_merge.falkordb_client import GraphHandle

__all__ = [
    "read_baseline_capabilities",
    "read_baseline_incoming_edges",
    "read_baseline_instrument_ids",
]

_CAPABILITY_QUERY = (
    "MATCH (c:Capability) WHERE c.id IN $ids "
    "RETURN c.id, c.name, c.confidence, c.description, c.embedding"
)
_INSTRUMENT_QUERY = "MATCH (r:RegulatoryInstrument) RETURN r.id"
_REQUIRES_QUERY = (
    "MATCH (s:Obligation)-[:REQUIRES]->(c:Capability) WHERE c.id IN $ids RETURN s.id, c.id"
)
_COVERS_QUERY = (
    "MATCH (s:PracticeArea)-[:COVERS]->(c:Capability) WHERE c.id IN $ids RETURN s.id, c.id"
)
_MITIGATED_BY_QUERY = (
    "MATCH (s:RiskPath)-[:MITIGATED_BY]->(c:Capability) WHERE c.id IN $ids RETURN s.id, c.id"
)
# Relationship type -> the read of that type's incoming Capability edges.
_INCOMING_EDGE_QUERIES: dict[str, str] = {
    "REQUIRES": _REQUIRES_QUERY,
    "COVERS": _COVERS_QUERY,
    "MITIGATED_BY": _MITIGATED_BY_QUERY,
}


def read_baseline_capabilities(
    graph: GraphHandle, ids: Collection[str]
) -> dict[str, BaselineCapability]:
    """Read the Capability nodes with the given ids from one baseline graph.

    Args:
        graph: Read handle on one `{short}_baseline` graph.
        ids: Capability ids to look for.

    Returns:
        The Capabilities the baseline holds, by id; empty when it holds none of them (no read is
        made when `ids` is empty).

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    if not ids:
        return {}
    rows = query_rows(graph, _CAPABILITY_QUERY, {"ids": sorted(ids)})
    return {
        cast("str", row[0]): BaselineCapability(
            id=cast("str", row[0]),
            name=cast("str", row[1]),
            confidence=cast("float | None", row[2]),
            description=cast("str | None", row[3]),
            embedding=tuple(cast("list[float]", row[4])) if row[4] is not None else None,
        )
        for row in rows
    }


def read_baseline_instrument_ids(graph: GraphHandle) -> tuple[str, ...]:
    """Read the id of every `RegulatoryInstrument` one baseline graph holds.

    The instrument id is read from the baseline itself rather than derived from the graph name,
    because the graph name is built from a short name that need not match the instrument id.

    Args:
        graph: Read handle on one `{short}_baseline` graph.

    Returns:
        The instrument ids, sorted; empty when the baseline holds none.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    return tuple(sorted(cast("str", row[0]) for row in query_rows(graph, _INSTRUMENT_QUERY)))


def read_baseline_incoming_edges(
    graph: GraphHandle, rel_type: str, ids: Collection[str]
) -> tuple[tuple[str, str], ...]:
    """Read one baseline's incoming `rel_type` edges into the Capabilities with the given ids.

    Args:
        graph: Read handle on one `{short}_baseline` graph.
        rel_type: The incoming relationship type: `REQUIRES` (from an Obligation), `COVERS`
            (from a PracticeArea) or `MITIGATED_BY` (from a RiskPath).
        ids: Target Capability ids.

    Returns:
        `(source id, target id)` of each edge, in read order; empty when there is none (no read
        is made when `ids` is empty).

    Raises:
        KeyError: `rel_type` is not a relationship type read here.
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    query = _INCOMING_EDGE_QUERIES[rel_type]
    if not ids:
        return ()
    rows = query_rows(graph, query, {"ids": sorted(ids)})
    return tuple((cast("str", row[0]), cast("str", row[1])) for row in rows)
