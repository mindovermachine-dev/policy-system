"""Domain-specific exception types for `ps_service.company_merge`.

Mirrors `ps_service.domain_mapper.errors`: one exception type per distinct failure boundary
this component owns, so callers can catch a specific failure instead of a generic
`Exception` or `ValueError`.
"""

from __future__ import annotations


class CompanyMergeConfigurationError(Exception):
    """A Capability merge was attempted without usable merge and review thresholds.

    Either threshold being `None` means `PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD` or
    `PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD` was never set or resolved via
    `ServiceConfig`; a review threshold above the merge threshold is also refused
    (`capability_config.capability_thresholds`). Also raised by
    `falkordb_client.check_connectivity` when FalkorDB cannot be reached. `merge.py` raises it
    before any graph call is made. The caller should fix the configuration or start FalkorDB.
    """


class CompanyMergePersistenceError(Exception):
    """A FalkorDB write for the single-tenant graph could not be completed safely.

    For example, an edge rewiring write references a canonical id with no
    corresponding `CanonicalResolution`. `graph_writer.py` raises it before any group is
    submitted for the offending write; it is also raised when the Graph Write Gateway refuses
    a group or cannot confirm it was applied. The graph may be partly written only if the
    gateway applied earlier groups; the caller should not assume the failed write took effect.
    """


class GovernanceOwnerRequiredError(Exception):
    """A baseline carries Policy/Standard/Control content but no importing owner was supplied.

    Raised by `governance_import.import_draft_governance` before any
    write. An imported tree is created `draft`, owned by whoever imports it; an
    ownerless draft would be unreachable by the propose/edit flow, so the import
    is refused instead. The message is fixed and domain-level.
    """


class CompanyMergeValidationError(Exception):
    """An input to a pure `company_merge` computation is malformed.

    For example, `similarity.cosine_similarity` was given vectors of
    mismatched length, or a zero-magnitude vector for which cosine similarity
    is undefined. Also raised by `dedup.py` and `redirects.py` for malformed
    inputs. `pending_review.resolve_review` raises it for a decision other than `keep-separate`.
    It is raised before any result is computed; the caller should correct the input.
    """


class UnknownReviewKindError(CompanyMergeValidationError):
    """A review names a node kind that has no registered `ReviewableKind` spec.

    Raised by `reviewable_kinds.reviewable_kind` and `require_registered`, and so by every
    review read that checks the kind a stored `PendingReview` carries (list, get, resolve, merge
    sides) and every review writer that is handed an unregistered spec. The message names the
    kind, so a stray row can be found. Nothing is written or skipped: a review that cannot be
    understood must not be hidden, because hiding it could unblock an export. The API layer
    translates it to `ReviewKindUnsupportedError`; `ps_service.company_merge` never imports
    `ps_service.api`.
    """


class StalePendingReviewError(Exception):
    """A `PendingReview`'s referenced incoming/existing node no longer exists.

    Raised by `pending_review.read_review_merge_sides` when the read finds that `incoming_id` or
    `nearest_existing_id` no longer resolves to a live node, for example because an unrelated
    merge already absorbed it. It is raised before any merge approval exists, so the graph is
    unchanged. The API layer translates it to
    `PendingReviewNotFoundError`; `ps_service.company_merge` never imports `ps_service.api`.

    Attributes:
        review_id: Id of the pending review whose reference went stale.
    """

    def __init__(self, review_id: str) -> None:
        """Keep `review_id` so the API layer can build its own message.

        Args:
            review_id: Id of the stale pending review.
        """
        self.review_id = review_id
        super().__init__(f"pending review {review_id!r} references a node that no longer exists")
