"""Precondition for exporting an instrument: it must have no unresolved near misses.

A curated instrument must not ship while a Company Merge near-miss review still touches one of
its Capabilities: the review lives in the `policy_system` graph, which export never serializes,
so the decision would be lost from the catalog artifact. Company Merge records a near miss only
when the incoming Capability is minted under its own `{short}_baseline` id, so an id match
between a review and the instrument's baseline Capabilities finds exactly the instrument's
reviews without walking the regulatory spine.

"Unresolved" is the predicate `near_misses_list` uses, because this gate reads the reviews
through `list_pending_reviews`: both referenced nodes exist under the review's kind (an absorbed
node is deleted, so a review naming one is not unresolved). The gate issues no Cypher of its own
for reviews, so the predicate has one definition and cannot drift.

Public functions:
    `unresolved_near_miss_review_ids`: the sorted ids of the reviews that block an export.

The module returns ids and never raises a domain error and never imports `ps_service.api`, so
the service route and the maintainer script each report a refusal in their own way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ps_service.company_merge.pending_review import list_pending_reviews

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.export.falkordb_connection import (
        _GraphQueryHandle,  # pyright: ignore[reportPrivateUsage]
    )

__all__ = ["unresolved_near_miss_review_ids"]

_CAPABILITY_IDS_QUERY = "MATCH (c:Capability) RETURN c.id"


def unresolved_near_miss_review_ids(
    baseline: _GraphQueryHandle, single_tenant: GraphHandle
) -> tuple[str, ...]:
    """Return the ids of the unresolved reviews that touch the instrument's Capabilities.

    A review touches the instrument when its `incoming_id` or its `nearest_existing_id` is the
    id of a `Capability` in the instrument's `{short}_baseline` graph. The review's `kind` is not
    consulted: a Policy review cannot carry a Capability id.

    Args:
        baseline: Read handle on the instrument's `{short}_baseline` graph.
        single_tenant: Read handle on the single-tenant `policy_system` graph.

    Returns:
        The matching review ids, sorted, each once; empty when nothing blocks the export.

    Raises:
        Exception: Errors from reading either graph (for example a FalkorDB connection error)
            propagate unchanged, so the caller fails closed.

    Side Effects:
        None; reads only. The caller logs the outcome, so a refused export emits exactly one
        export log entry.
    """
    capability_rows = cast("list[list[object]]", baseline.query(_CAPABILITY_IDS_QUERY).result_set)  # pyright: ignore[reportAttributeAccessIssue]
    capability_ids = {str(row[0]) for row in capability_rows}
    blocking = sorted(
        {
            review.id
            for review in list_pending_reviews(single_tenant)
            if review.incoming_id in capability_ids or review.nearest_existing_id in capability_ids
        }
    )
    return tuple(blocking)
