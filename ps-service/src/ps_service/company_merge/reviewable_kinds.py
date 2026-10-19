"""The node kinds that can raise a near miss, each described once.

A `ReviewableKind` states what Company Merge needs to know to compare nodes of one label and to
review the pairs it cannot decide: the node label, the property holding the node's own text, the
scope its candidates are drawn from, and the rule that decides what a close pair does. Detection
(`dedup`), review persistence and reading (`pending_review`) and the export gate
(`export.near_miss_gate`) all read the spec; none of them names a label itself.

`CAPABILITY` is registered when this module is imported. Another kind joins by calling
`register_reviewable_kind`. A `PendingReview` can only carry the label of a registered kind:
`reviewable_kind` looks a stored label up and raises `UnknownReviewKindError` naming it otherwise.

The thresholds a rule needs are not part of the spec. They travel beside it as a
`ReviewThresholds`, and `require_thresholds` checks that the ones the rule needs are present.

The label and the text property are interpolated into Cypher, so a spec validates both against an
identifier pattern when it is built. They are code-owned constants, never adapter-, LLM- or
user-sourced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

from ps_service.company_merge.errors import (
    CompanyMergeConfigurationError,
    CompanyMergeValidationError,
    UnknownReviewKindError,
)

__all__ = [
    "CAPABILITY",
    "REVIEWABLE_KINDS",
    "ReviewRule",
    "ReviewScope",
    "ReviewThresholds",
    "ReviewableKind",
    "register_reviewable_kind",
    "registered_reviewable_kinds",
    "require_registered",
    "require_thresholds",
    "reviewable_kind",
]

type ReviewScope = Literal["global", "per-instrument"]
type ReviewRule = Literal["merge-above", "review-only"]

_LABEL_PATTERN: Final = re.compile(r"[A-Z][A-Za-z0-9]*")
_TEXT_PROPERTY_PATTERN: Final = re.compile(r"[a-z][a-z_0-9]*")


@dataclass(frozen=True, slots=True)
class ReviewableKind:
    """One node kind that can raise a near miss.

    Attributes:
        label: The node label, for example `"Capability"`. Starts with a capital letter and holds
            letters and digits only.
        text_property: The property holding the node's own text, for example `"name"`. Lower case
            letters, digits and underscores, starting with a letter.
        scope: Where candidates come from: `"global"` is every node of the label;
            `"per-instrument"` is the nodes the run's instrument `DEFINES`, plus the run's own
            mints.
        rule: What a close pair does: `"merge-above"` merges onto an existing node at or above
            the merge threshold and reviews the rest; `"review-only"` never merges and reviews
            pairs at or above the review threshold.

    Raises:
        CompanyMergeValidationError: `label` or `text_property` is not a plain identifier.
    """

    label: str
    text_property: str
    scope: ReviewScope
    rule: ReviewRule

    def __post_init__(self) -> None:
        """Refuse a label or text property that could not be interpolated into Cypher safely."""
        if _LABEL_PATTERN.fullmatch(self.label) is None:
            raise CompanyMergeValidationError(
                f"reviewable kind label {self.label!r} is not a plain node label"
            )
        if _TEXT_PROPERTY_PATTERN.fullmatch(self.text_property) is None:
            raise CompanyMergeValidationError(
                f"{self.label}: text_property {self.text_property!r} is not a plain property name"
            )


@dataclass(frozen=True, slots=True)
class ReviewThresholds:
    """The two similarity lines a rule can use; each rule requires its own.

    Attributes:
        merge: The auto-merge line: an eligible candidate scoring at or above it is merged onto.
            Only a `"merge-above"` kind takes one.
        review: The near-miss line: a pair scoring at or above it is reviewed and a pair below it
            is not. `None` means no floor for a `"merge-above"` kind (each best neighbour that
            was scored is reviewed; Capability's production path always sets one, built by
            `capability_config.capability_thresholds`); a `"review-only"` kind requires one.
    """

    merge: float | None = None
    review: float | None = None


CAPABILITY: Final = ReviewableKind("Capability", "name", "global", "merge-above")

REVIEWABLE_KINDS: dict[str, ReviewableKind] = {}
"""Registered specs by label; written only through `register_reviewable_kind`."""


def register_reviewable_kind(spec: ReviewableKind) -> None:
    """Register `spec` so reviews of its label are understood everywhere.

    Args:
        spec: The kind to register.

    Raises:
        CompanyMergeValidationError: A kind with the same label is already registered.

    Side Effects:
        Adds `spec` to the process-wide registry.
    """
    if spec.label in REVIEWABLE_KINDS:
        raise CompanyMergeValidationError(
            f"a reviewable kind is already registered for {spec.label!r}"
        )
    REVIEWABLE_KINDS[spec.label] = spec


def reviewable_kind(label: str) -> ReviewableKind:
    """Return the spec registered for `label`.

    Args:
        label: A node label, as stored on a `PendingReview`.

    Returns:
        The registered spec.

    Raises:
        UnknownReviewKindError: No kind is registered for `label`; the message names it.
    """
    spec = REVIEWABLE_KINDS.get(label)
    if spec is None:
        raise UnknownReviewKindError(f"no reviewable kind is registered for {label!r}")
    return spec


def registered_reviewable_kinds() -> tuple[ReviewableKind, ...]:
    """Return every registered spec, in registration order.

    Returns:
        The registered specs; always includes `CAPABILITY`.
    """
    return tuple(REVIEWABLE_KINDS.values())


def require_registered(spec: ReviewableKind) -> ReviewableKind:
    """Return `spec` once it is confirmed to be the one registered for its label.

    Args:
        spec: A spec a caller was handed.

    Returns:
        `spec`, unchanged.

    Raises:
        UnknownReviewKindError: `spec` is not registered, or a different spec holds its label;
            the message names the label.
    """
    if REVIEWABLE_KINDS.get(spec.label) is not spec:
        raise UnknownReviewKindError(f"no reviewable kind is registered for {spec.label!r}")
    return spec


def require_thresholds(spec: ReviewableKind, thresholds: ReviewThresholds) -> None:
    """Check that `thresholds` hold what `spec`'s rule needs and nothing it cannot use.

    A `"merge-above"` kind needs a merge threshold and may have a review threshold. A
    `"review-only"` kind needs a review threshold and has nothing to merge, so a merge threshold is
    refused rather than ignored.

    Args:
        spec: The kind the thresholds are for.
        thresholds: The thresholds a caller supplied.

    Raises:
        CompanyMergeConfigurationError: A required threshold is missing, or a merge threshold was
            given to a `"review-only"` kind. The message names the kind.
    """
    if spec.rule == "merge-above" and thresholds.merge is None:
        raise CompanyMergeConfigurationError(
            f"{spec.label}: rule {spec.rule!r} requires a merge threshold"
        )
    if spec.rule == "review-only" and thresholds.merge is not None:
        raise CompanyMergeConfigurationError(
            f"{spec.label}: rule {spec.rule!r} takes no merge threshold"
        )
    if spec.rule == "review-only" and thresholds.review is None:
        raise CompanyMergeConfigurationError(
            f"{spec.label}: rule {spec.rule!r} requires a review threshold"
        )


register_reviewable_kind(CAPABILITY)
