"""Domain-specific exception types for `ps_service.company_merge`.

Mirrors `ps_service.domain_mapper.errors`'s shape (PLAN_REVIEWED.md §1): one
exception type per distinct failure boundary this component owns, never a
generic `Exception`/`ValueError` (L1 Error Handling, L2 Error Handling).
"""

from __future__ import annotations


class CompanyMergeConfigurationError(Exception):
    """`merge_baseline_graph` was called without a resolved similarity threshold.

    `similarity_threshold is None` means `PS_COMPANYMERGE_SIMILARITY_THRESHOLD`
    was never set/resolved via `ServiceConfig` (PLAN_REVIEWED.md §7 step 0,
    §8's B1 fix). Raised by `merge.py`, before any graph call of any kind is
    made.
    """


class CompanyMergePersistenceError(Exception):
    """A FalkorDB write for the single-tenant graph could not be completed safely.

    For example, an edge rewiring write references a canonical id with no
    corresponding `CanonicalResolution` (PLAN_REVIEWED.md §6). Raised by
    `graph_writer.py`, before any `graph.query()` call is made for the
    offending write.
    """


class CompanyMergeValidationError(Exception):
    """An input to a pure `company_merge` computation is malformed.

    For example, `similarity.cosine_similarity` was given vectors of
    mismatched length, or a zero-magnitude vector for which cosine similarity
    is undefined. Raised by `similarity.py`, before any similarity score is
    computed.
    """


class StalePendingReviewError(Exception):
    """A `PendingReview`'s referenced incoming/existing node no longer exists.

    Issue #35 Slice 4, CHANGES.md H2: raised by
    `pending_review.resolve_review` (`decision="merge"`) when the combined
    existence-check read finds either `incoming_id` or `nearest_existing_id`
    no longer resolves to a real node -- e.g. a prior, unrelated merge
    already deleted it (the review is "stale"). Raised **before** the merge
    write query is ever issued, so AC-BI-008's "no graph changes" half holds
    on this path too. The API-boundary translation to
    `PendingReviewNotFoundError` (with H2's dedicated stale-reference
    message) happens in `api.near_miss_review_orchestration.
    run_resolve_near_miss` -- mirrors `RestoreArtifactRejectedError`'s own
    "API-boundary translation of a lower-layer condition" pattern;
    `ps_service.company_merge` never imports `ps_service.api` (M6 layering
    stays one-directional).
    """

    def __init__(self, review_id: str) -> None:
        """Store `review_id` for the API-boundary layer's message-building."""
        self.review_id = review_id
        super().__init__(f"pending review {review_id!r} references a node that no longer exists")
