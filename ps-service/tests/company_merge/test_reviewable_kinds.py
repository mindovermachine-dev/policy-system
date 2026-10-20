"""`reviewable_kinds`: the one place a node kind that can raise a near miss is described."""

from __future__ import annotations

import pytest

from company_merge._kinds import UNREGISTERED_KIND
from ps_service.company_merge import pending_review
from ps_service.company_merge.errors import (
    CompanyMergeConfigurationError,
    CompanyMergeValidationError,
    UnknownReviewKindError,
)
from ps_service.company_merge.reviewable_kinds import (
    CAPABILITY,
    REVIEWABLE_KINDS,
    ROLE,
    ReviewableKind,
    ReviewThresholds,
    register_reviewable_kind,
    registered_reviewable_kinds,
    require_registered,
    require_thresholds,
    reviewable_kind,
)

_MERGE_ABOVE = ReviewableKind("Widget", "name", "global", "merge-above")
_REVIEW_ONLY = ReviewableKind("Gadget", "name", "per-instrument", "review-only")


def test_lookup_of_an_unregistered_kind_names_it() -> None:
    with pytest.raises(UnknownReviewKindError, match=UNREGISTERED_KIND):
        reviewable_kind(UNREGISTERED_KIND)


def test_an_unknown_kind_error_is_a_validation_error() -> None:
    assert issubclass(UnknownReviewKindError, CompanyMergeValidationError)


def test_capability_and_role_are_the_registered_kinds_in_production() -> None:
    assert registered_reviewable_kinds() == (CAPABILITY, ROLE)
    assert reviewable_kind("Capability") is CAPABILITY
    assert (CAPABILITY.label, CAPABILITY.text_property) == ("Capability", "name")
    assert (CAPABILITY.scope, CAPABILITY.rule) == ("global", "merge-above")


@pytest.mark.parametrize(
    "label", ["", "capability", "Cap ability", "Cap-ability", "1Cap", "A) DETACH"]
)
def test_spec_label_must_be_an_identifier(label: str) -> None:
    with pytest.raises(CompanyMergeValidationError, match="label"):
        ReviewableKind(label, "name", "global", "merge-above")


@pytest.mark.parametrize("text_property", ["", "Name", "na me", "name.x", "1name", "n} RETURN"])
def test_spec_text_property_must_be_an_identifier(text_property: str) -> None:
    with pytest.raises(CompanyMergeValidationError, match="text_property"):
        ReviewableKind("Widget", text_property, "global", "merge-above")


def test_duplicate_registration_is_refused_naming_the_kind() -> None:
    with pytest.raises(CompanyMergeValidationError, match="Capability"):
        register_reviewable_kind(ReviewableKind("Capability", "name", "global", "merge-above"))


def test_a_registered_kind_is_found_by_its_label(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(REVIEWABLE_KINDS, _MERGE_ABOVE.label, _MERGE_ABOVE)
    monkeypatch.delitem(REVIEWABLE_KINDS, _MERGE_ABOVE.label)

    register_reviewable_kind(_MERGE_ABOVE)

    assert reviewable_kind("Widget") is _MERGE_ABOVE
    assert registered_reviewable_kinds() == (CAPABILITY, ROLE, _MERGE_ABOVE)


def test_test_only_kind_fixture_registers_the_kind(
    test_only_reviewable_kind: ReviewableKind,
) -> None:
    assert test_only_reviewable_kind == ReviewableKind(
        "ProbeItem", "name", "per-instrument", "review-only"
    )
    assert reviewable_kind("ProbeItem") is test_only_reviewable_kind


def test_test_only_kind_fixture_reverts_after_the_test() -> None:
    """Runs after the fixture test above: a leaked registration would be found here."""
    assert "ProbeItem" not in {kind.label for kind in registered_reviewable_kinds()}


def test_require_registered_accepts_the_registered_spec() -> None:
    assert require_registered(CAPABILITY) is CAPABILITY


def test_require_registered_refuses_a_spec_that_is_not_registered_naming_it() -> None:
    with pytest.raises(UnknownReviewKindError, match="Widget"):
        require_registered(_MERGE_ABOVE)


def test_require_registered_refuses_a_look_alike_of_a_registered_label() -> None:
    look_alike = ReviewableKind("Capability", "title", "global", "merge-above")

    with pytest.raises(UnknownReviewKindError, match="Capability"):
        require_registered(look_alike)


def test_merge_above_needs_a_merge_threshold() -> None:
    with pytest.raises(
        CompanyMergeConfigurationError,
        match=r"Widget: rule 'merge-above' requires a merge threshold",
    ):
        require_thresholds(_MERGE_ABOVE, ReviewThresholds())


@pytest.mark.parametrize("review", [None, 0.5])
def test_merge_above_accepts_a_merge_threshold_with_or_without_a_review_threshold(
    review: float | None,
) -> None:
    require_thresholds(_MERGE_ABOVE, ReviewThresholds(merge=0.85, review=review))


def test_review_only_needs_a_review_threshold() -> None:
    with pytest.raises(
        CompanyMergeConfigurationError,
        match=r"Gadget: rule 'review-only' requires a review threshold",
    ):
        require_thresholds(_REVIEW_ONLY, ReviewThresholds())


def test_review_only_refuses_a_merge_threshold() -> None:
    with pytest.raises(
        CompanyMergeConfigurationError,
        match=r"Gadget: rule 'review-only' takes no merge threshold",
    ):
        require_thresholds(_REVIEW_ONLY, ReviewThresholds(merge=0.85, review=0.7))


def test_review_only_accepts_a_review_threshold_alone() -> None:
    require_thresholds(_REVIEW_ONLY, ReviewThresholds(review=0.7))


def test_thresholds_default_to_none() -> None:
    assert ReviewThresholds() == ReviewThresholds(merge=None, review=None)


def test_the_pending_review_docstring_names_every_registered_kind() -> None:
    """The module docstring must not drift from the kinds that really exist."""
    doc = pending_review.__doc__ or ""
    missing = [kind.label for kind in registered_reviewable_kinds() if kind.label not in doc]

    assert missing == []
