"""The guards that turn the near-miss settings into `ReviewThresholds`, one per reviewable kind.

Every production path that deduplicates Capabilities or compares Roles (the ingest merge and the
restore merge) resolves its thresholds here, so a missing or inconsistent setting fails the same
way in both before any graph is touched.
"""

from __future__ import annotations

from ps_service.company_merge.errors import CompanyMergeConfigurationError
from ps_service.company_merge.reviewable_kinds import ReviewThresholds

_MERGE_VARIABLE = "PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD"
_REVIEW_VARIABLE = "PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD"
_ROLE_REVIEW_VARIABLE = "PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD"


def capability_thresholds(
    merge_threshold: float | None, review_threshold: float | None
) -> ReviewThresholds:
    """Build the Capability thresholds from the two configured settings.

    Args:
        merge_threshold: Similarity at or above which an eligible match is merged
            (`PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD`); `None` when unset.
        review_threshold: Similarity at or above which a near miss is raised for review
            (`PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD`); `None` when unset. `0.0` hides
            nothing.

    Returns:
        `ReviewThresholds(merge=merge_threshold, review=review_threshold)`.

    Raises:
        CompanyMergeConfigurationError: Either value is `None` (the message names each unset
            variable), or `review_threshold` is above `merge_threshold` (names both).
    """
    unset = [
        name
        for name, value in (
            (_MERGE_VARIABLE, merge_threshold),
            (_REVIEW_VARIABLE, review_threshold),
        )
        if value is None
    ]
    if merge_threshold is None or review_threshold is None:
        raise CompanyMergeConfigurationError(
            f"{' and '.join(unset)} must be set -- no default is defined; resolve it via "
            "ServiceConfig before merging Capabilities"
        )
    if review_threshold > merge_threshold:
        raise CompanyMergeConfigurationError(
            f"{_REVIEW_VARIABLE} ({review_threshold}) must not exceed "
            f"{_MERGE_VARIABLE} ({merge_threshold})"
        )
    return ReviewThresholds(merge=merge_threshold, review=review_threshold)


def role_thresholds(review_threshold: float | None) -> ReviewThresholds:
    """Build the Role thresholds from the configured review setting.

    A Role is review-only: it is never merged automatically, so there is no merge threshold.

    Args:
        review_threshold: Similarity at or above which a Role near miss is raised for review
            (`PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD`); `None` when unset. `0.0` hides nothing.

    Returns:
        `ReviewThresholds(merge=None, review=review_threshold)`.

    Raises:
        CompanyMergeConfigurationError: `review_threshold` is `None` (the message names the
            variable).
    """
    if review_threshold is None:
        raise CompanyMergeConfigurationError(
            f"{_ROLE_REVIEW_VARIABLE} must be set -- no default is defined; resolve it via "
            "ServiceConfig before comparing Roles"
        )
    return ReviewThresholds(merge=None, review=review_threshold)
