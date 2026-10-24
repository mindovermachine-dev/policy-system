"""Models and errors of a near-miss merge batch.

A batch is a hand-picked set of near-miss reviews merged under one passkey approval. The officer
sees one preview entry per review, the approval signs one item per review, and signing produces one
result per review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.models import CapabilityMergePreview, RoleMergePreview

__all__ = [
    "TOOL_MERGE_NEAR_MISSES",
    "AcknowledgmentNeeded",
    "NearMissBatchAcknowledgmentRequiredError",
    "NearMissBatchApproval",
    "NearMissBatchRefusedError",
    "NearMissBatchStatus",
    "NearMissMergeItem",
    "NearMissMergePreviewEntry",
    "NearMissMergeResult",
    "ReviewFinding",
]

TOOL_MERGE_NEAR_MISSES: Final = "merge-near-misses"
"""Tool name of a near-miss merge batch approval; its executor merges every signed item."""


class NearMissMergeItem(BaseModel):
    """One merge a batch approval signs.

    Attributes:
        review_id: Id of the near-miss review the merge resolves.
        kind: Label of the review's node kind (`Capability` or `Role`).
        survivor_id: Id of the node that remains.
        absorbed_id: Id of the node that is deleted and replaced by a marker.
        state_digest: sha256 of the previewed state; binds the item to what the officer saw.
        acknowledged: True when the officer accepted the governance change of the merge.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str
    kind: str
    survivor_id: str
    absorbed_id: str
    state_digest: str
    acknowledged: bool

    def signed_form(self) -> dict[str, object]:
        """The item as it is signed: the argument names the per-kind merge executors read."""
        return {
            "review_id": self.review_id,
            "kind": self.kind,
            "survivor_id": self.survivor_id,
            "absorbed_id": self.absorbed_id,
            "state_digest": self.state_digest,
            "acknowledge_governance_change": self.acknowledged,
        }


class NearMissMergePreviewEntry(BaseModel):
    """What the officer is shown for one merge of a batch.

    Attributes:
        review_id: Id of the near-miss review the merge resolves.
        kind: Label of the review's node kind.
        acknowledged: True when the officer accepted the governance change of this merge.
        preview: The per-kind merge preview.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str
    kind: str
    acknowledged: bool
    preview: CapabilityMergePreview | RoleMergePreview

    def wire_form(self) -> dict[str, object]:
        """The entry as the tool returns it: the review fields, then the preview's fields."""
        return {
            "review_id": self.review_id,
            "kind": self.kind,
            "acknowledged": self.acknowledged,
            **self.preview.model_dump(mode="json"),
        }


@dataclass(frozen=True, slots=True)
class NearMissBatchApproval:
    """A previewed batch and the pending passkey approval that binds it.

    Attributes:
        merges: One preview entry per review, in the order the officer selected them.
        pending_approval_id: Id of the pending approval row.
        approval_url: Link where the officer signs with a passkey; includes the one-time code.
        expires_at: ISO 8601 time after which the approval can no longer be signed.
    """

    merges: tuple[NearMissMergePreviewEntry, ...]
    pending_approval_id: str
    approval_url: str
    expires_at: str  # ISO 8601


class NearMissMergeResult(BaseModel):
    """The outcome of one merge of a signed batch.

    Attributes:
        review_id: Id of the near-miss review the merge resolved.
        kind: Label of the review's node kind.
        state: `merged`, `failed`, or `unsettled` (the outcome is not known yet).
        winner_id: Id of the surviving node once merged, else None.
        loser_id: Id of the absorbed node once merged, else None.
        error: Safe text saying why the merge was not carried out, else None.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str
    kind: str
    state: Literal["merged", "failed", "unsettled"]
    winner_id: str | None = None
    loser_id: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class NearMissBatchStatus:
    """The creator's view of a batch approval.

    Attributes:
        pending_approval_id: Id of the approval row.
        status: `signed` once signed; otherwise `expired` after `expires_at`, else `pending`.
        merges: One result per review once signed; empty before.
    """

    pending_approval_id: str
    status: Literal["pending", "expired", "signed"]
    merges: tuple[NearMissMergeResult, ...]


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """One review a batch request cannot include, with the reason in words.

    Attributes:
        review_id: Id of the offending review.
        state: Why the review cannot be merged, written for the officer.
    """

    review_id: str
    state: str


class NearMissBatchRefusedError(GraphCleanupValidationError):
    """A batch request cannot be previewed; every offending review is named, nothing is created.

    Attributes:
        findings: The offending reviews with their named states, in selection order.
    """

    def __init__(self, message: str, findings: tuple[ReviewFinding, ...] = ()) -> None:
        """Keep the findings beside the officer-facing message.

        Args:
            message: The text shown to the officer; lists every finding.
            findings: The offending reviews with their named states.
        """
        super().__init__(message)
        self.findings = findings


@dataclass(frozen=True, slots=True)
class AcknowledgmentNeeded:
    """A Capability review whose merge changes what a Policy governs and is not yet acknowledged.

    Attributes:
        review_id: Id of the review.
        policy_id: Id of the Policy whose governed set changes.
        policy_title: Title of that Policy.
        policy_status: Lifecycle status of that Policy.
    """

    review_id: str
    policy_id: str
    policy_title: str
    policy_status: str

    def wire_form(self) -> dict[str, object]:
        """The entry as the tool returns it."""
        return {
            "review_id": self.review_id,
            "policy": {
                "id": self.policy_id,
                "title": self.policy_title,
                "status": self.policy_status,
            },
        }


class NearMissBatchAcknowledgmentRequiredError(Exception):
    """Some selected reviews change governance and lack their acknowledgment; no approval exists.

    Attributes:
        reviews: The reviews that need an acknowledgment, with the Policy each one affects.
        merges: The preview of every selected review, so the officer can read the whole set.
    """

    def __init__(
        self,
        message: str,
        reviews: tuple[AcknowledgmentNeeded, ...],
        merges: tuple[NearMissMergePreviewEntry, ...],
    ) -> None:
        """Keep the affected reviews and the previews beside the officer-facing message.

        Args:
            message: The text shown to the officer; names each review and Policy.
            reviews: The reviews that need an acknowledgment.
            merges: The preview of every selected review, in selection order.
        """
        super().__init__(message)
        self.reviews = reviews
        self.merges = merges
