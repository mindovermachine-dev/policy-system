"""Company Merge's own precondition check on a `{short}_baseline` graph.

Issue #28 (AC-BI-002/AC-BI-003): before `merge_baseline_graph` runs against
the real, shared `policy_system` graph, Domain Mapper's structural guarantee
(#15) -- exactly one `HAS` edge per Obligation -- is reconfirmed live,
mirroring how `merge.py` already treats `similarity_threshold is None` as a
precondition it itself enforces (`CompanyMergeConfigurationError`) rather
than something upstream enforces on Company Merge's behalf. Placed in
`company_merge`, not `domain_mapper`: it is Company Merge, not Domain
Mapper, that has a new reason to re-check this invariant here (#34's
precedent -- a live-capstone re-run against an already-populated baseline
can silently violate it after the fact).

Read-only: the single query issued here is a `MATCH ... OPTIONAL MATCH ...
RETURN ...`, never a `MERGE`/`SET`/`DELETE`.
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
    """One Obligation whose inbound `HAS`-edge count is not exactly 1."""

    obligation_id: str
    has_edge_count: int


def check_obligation_has_edge_cardinality(
    baseline_graph: GraphHandle,
) -> tuple[ObligationHasEdgeViolation, ...]:
    """Reconfirm Domain Mapper's structural guarantee (#15) for AC-BI-002/003.

    Issues the exact exhaustive query #15's/#34's own verification used
    (`ps-service/tests/domain_mapper/test_live_capstone.py:232`):
    `MATCH (o:Obligation) OPTIONAL MATCH (:Role)-[h:HAS]->(o) RETURN o.id,
    count(h)` -- never a paraphrase. Returns one `ObligationHasEdgeViolation`
    per Obligation whose `HAS`-edge count is not exactly 1, in query-result
    order; an empty tuple means the invariant holds. A baseline graph with
    zero Obligations returns an empty tuple, not an error -- there is
    nothing to violate.
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
