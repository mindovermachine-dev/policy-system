"""Precondition for exporting an instrument: it must have no unresolved near misses.

A curated instrument must not ship while a Company Merge near-miss review still touches one of
its nodes: the review lives in the `policy_system` graph, which export never serializes, so the
decision would be lost from the catalog artifact. Company Merge records a near miss only when the
incoming node is minted under its own `{short}_baseline` id, so an id match between a review and
the baseline nodes of the review's own kind finds exactly the instrument's reviews without walking
the regulatory spine. Every registered reviewable kind is covered: the gate asks the registry which
labels can carry a review, so a new kind blocks export without a change here.

"Unresolved" is the predicate `near_misses_list` uses, because this gate reads the reviews
through `list_pending_reviews`: both referenced nodes exist under the review's kind (an absorbed
node is deleted, so a review naming one is not unresolved). The gate issues no Cypher of its own
for reviews, so the predicate has one definition and cannot drift. A review whose kind has no
registered spec is refused by that read, naming the kind, so it cannot be hidden from the gate.

Public functions:
    `unresolved_near_miss_review_ids`: the sorted ids of the reviews that block an export.

The module returns ids and never imports `ps_service.api`, so the service route and the
maintainer script each report a refusal in their own way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ps_service.company_merge.pending_review import list_pending_reviews
from ps_service.company_merge.reviewable_kinds import registered_reviewable_kinds

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.reviewable_kinds import ReviewableKind
    from ps_service.export.falkordb_connection import (
        _GraphQueryHandle,  # pyright: ignore[reportPrivateUsage]
    )

__all__ = ["unresolved_near_miss_review_ids"]


def _baseline_node_ids(baseline: _GraphQueryHandle, spec: ReviewableKind) -> frozenset[str]:
    """Read the ids of the baseline nodes carrying `spec`'s label.

    The label is interpolated: it is a registered spec's label, validated as a plain identifier
    when the spec was built, never adapter-, LLM- or user-sourced.
    """
    rows = cast(
        "list[list[object]]",
        baseline.query(f"MATCH (n:{spec.label}) RETURN n.id").result_set,  # pyright: ignore[reportAttributeAccessIssue]
    )
    return frozenset(str(row[0]) for row in rows)


def unresolved_near_miss_review_ids(
    baseline: _GraphQueryHandle, single_tenant: GraphHandle
) -> tuple[str, ...]:
    """Return the ids of the unresolved reviews that touch the instrument's nodes.

    A review touches the instrument when its `incoming_id` or its `nearest_existing_id` is the id
    of a node, in the instrument's `{short}_baseline` graph, with the label of the review's own
    kind. Ids of another kind never match: a Capability id equal to a Role id is no hit.

    The baseline ids of every registered kind are read first, so a failed baseline read refuses the
    export even when no review exists.

    Args:
        baseline: Read handle on the instrument's `{short}_baseline` graph.
        single_tenant: Read handle on the single-tenant `policy_system` graph.

    Returns:
        The matching review ids, sorted, each once; empty when nothing blocks the export.

    Raises:
        UnknownReviewKindError: A stored review has a kind with no registered spec; the message
            names the kind. The export is refused rather than the review skipped.
        Exception: Errors from reading either graph (for example a FalkorDB connection error)
            propagate unchanged, so the caller fails closed.

    Side Effects:
        None; reads only. The caller logs the outcome, so a refused export emits exactly one
        export log entry.
    """
    ids_by_label = {
        spec.label: _baseline_node_ids(baseline, spec) for spec in registered_reviewable_kinds()
    }
    blocking = sorted(
        {
            review.id
            for review in list_pending_reviews(single_tenant)
            if {review.incoming_id, review.nearest_existing_id} & ids_by_label[review.kind]
        }
    )
    return tuple(blocking)
