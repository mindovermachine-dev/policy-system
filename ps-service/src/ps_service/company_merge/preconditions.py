"""Live precondition check that every Obligation in a graph has exactly one `HAS` edge.

Company Merge runs `check_obligation_has_edge_cardinality` before merging a baseline into the
shared `policy_system` graph. Domain Mapper writes one inbound `HAS` edge per Obligation, but a
re-run against an already-populated baseline can break that invariant afterwards, so Company
Merge re-checks it itself rather than trusting upstream. This mirrors how `merge.py` enforces a
resolved `merge_threshold` itself.

Read-only: the single query issued is a `MATCH ... OPTIONAL MATCH ... RETURN ...`, never a
`MERGE`, `SET` or `DELETE`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle

_OBLIGATION_HAS_EDGE_CARDINALITY_QUERY = (
    "MATCH (o:Obligation) OPTIONAL MATCH (:Role)-[h:HAS]->(o) RETURN o.id, count(h)"
)


@dataclass(frozen=True, slots=True)
class ObligationHasEdgeViolation:
    """One Obligation whose inbound `HAS`-edge count is not exactly 1.

    Attributes:
        obligation_id: The `id` property of the offending Obligation node.
        has_edge_count: Number of inbound `Role -[HAS]-> Obligation` edges; any value except 1
            (0 means no owning Role, 2 or more means competing owners).
    """

    obligation_id: str
    has_edge_count: int


def check_obligation_has_edge_cardinality(
    baseline_graph: GraphHandle,
) -> tuple[ObligationHasEdgeViolation, ...]:
    """Find Obligations whose inbound `HAS`-edge count is not exactly 1.

    Guards the merge against a baseline where an Obligation has no owning Role or several.
    The query is exhaustive: `MATCH (o:Obligation) OPTIONAL MATCH (:Role)-[h:HAS]->(o) RETURN
    o.id, count(h)`. Read-only.

    Args:
        baseline_graph: Handle on the graph to check, normally a `{short}_baseline` graph.

    Returns:
        One `ObligationHasEdgeViolation` per offending Obligation, in query-result order. An
        empty tuple means the invariant holds, including when the graph has no Obligations.

    Raises:
        Exception: Whatever `baseline_graph.query` raises on a connection or query failure is
            propagated unchanged.
    """
    result = baseline_graph.query(_OBLIGATION_HAS_EDGE_CARDINALITY_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    violations: list[ObligationHasEdgeViolation] = []
    for obligation_id, has_edge_count in rows:
        count = cast("int", has_edge_count)
        if count != 1:
            violations.append(
                ObligationHasEdgeViolation(
                    obligation_id=cast("str", obligation_id), has_edge_count=count
                )
            )
    return tuple(violations)
