"""`capability_thresholds`: the one guard turning the two Capability settings into thresholds."""

from __future__ import annotations

import pytest

from ps_service.company_merge.capability_config import capability_thresholds
from ps_service.company_merge.errors import CompanyMergeConfigurationError
from ps_service.company_merge.reviewable_kinds import ReviewThresholds

_MERGE_VAR = "PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD"
_REVIEW_VAR = "PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD"


def test_both_set_builds_thresholds() -> None:
    assert capability_thresholds(0.9, 0.5) == ReviewThresholds(merge=0.9, review=0.5)


def test_review_equal_to_merge_is_accepted() -> None:
    assert capability_thresholds(0.9, 0.9) == ReviewThresholds(merge=0.9, review=0.9)


def test_review_of_zero_is_accepted() -> None:
    assert capability_thresholds(0.9, 0.0) == ReviewThresholds(merge=0.9, review=0.0)


def test_missing_merge_threshold_names_the_merge_variable() -> None:
    with pytest.raises(CompanyMergeConfigurationError, match=_MERGE_VAR):
        capability_thresholds(None, 0.5)


def test_missing_review_threshold_names_the_review_variable() -> None:
    with pytest.raises(CompanyMergeConfigurationError, match=_REVIEW_VAR):
        capability_thresholds(0.9, None)


def test_both_missing_names_both_variables() -> None:
    with pytest.raises(CompanyMergeConfigurationError) as raised:
        capability_thresholds(None, None)

    assert _MERGE_VAR in str(raised.value)
    assert _REVIEW_VAR in str(raised.value)


def test_review_above_merge_names_both_variables() -> None:
    with pytest.raises(CompanyMergeConfigurationError) as raised:
        capability_thresholds(0.6, 0.8)

    assert _MERGE_VAR in str(raised.value)
    assert _REVIEW_VAR in str(raised.value)
