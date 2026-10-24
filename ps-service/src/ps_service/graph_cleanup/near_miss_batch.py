"""Create one passkey approval that merges a hand-picked set of near-miss reviews.

The officer chooses the reviews; each is resolved to its two sides, the survivor is picked by the
unchanged near-miss survivor rule, and the per-kind cleanup preview is built. One approval then
binds every item (review, survivor, absorbed node, state digest, acknowledgment), so the signature
covers exactly the set the officer saw. Nothing here writes to the graph: the merges run in
`near_miss_batch_execution` when a human signs.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Final

from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.models import CapabilityMergePreview
from ps_service.graph_cleanup.near_miss_batch_models import (
    TOOL_MERGE_NEAR_MISSES,
    AcknowledgmentNeeded,
    NearMissBatchAcknowledgmentRequiredError,
    NearMissBatchApproval,
    NearMissBatchRefusedError,
    NearMissMergeItem,
    NearMissMergePreviewEntry,
    ReviewFinding,
)
from ps_service.graph_cleanup.service import preview_capability_merge, preview_role_merge
from ps_service.logging import emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.pending_review import ReviewMergeSides
    from ps_service.graph_cleanup.models import RoleMergePreview
    from ps_service.logging import LogEmitter
    from ps_service.passkey_signing.store import PendingApprovalStore

__all__ = ["create_near_miss_merge_batch"]

_COMPONENT: Final = "graph_cleanup"
_UNKNOWN_REVIEW: Final = "no unresolved near-miss review has this id"
_STALE_REVIEW: Final = (
    "the review is stale: one of its nodes no longer exists (it was already merged or removed)"
)
_ACK_NOT_SELECTED: Final = (
    "an acknowledgment was given for a review that is not among the selected reviews"
)
_ACK_ON_ROLE: Final = (
    "an acknowledgment was given for a {kind} review, which has no governance change"
)
_ACK_NOT_NEEDED: Final = "an acknowledgment was given but this merge makes no governance change"
_NO_MERGE: Final = "a {kind} review cannot be merged; keep it separate"


def _refusal(findings: Sequence[ReviewFinding]) -> NearMissBatchRefusedError:
    """The refusal that names every offending review with its state."""
    lines = "\n".join(f"- {finding.review_id!r}: {finding.state}" for finding in findings)
    return NearMissBatchRefusedError(
        f"the request was refused and no approval was created:\n{lines}", tuple(findings)
    )


def _check_shape(review_ids: Sequence[str]) -> None:
    """Refuse an empty selection and any review id given more than once."""
    if not review_ids:
        message = "no review ids were given; name at least one review to merge"
        raise NearMissBatchRefusedError(message)
    duplicates = sorted(r for r, count in Counter(review_ids).items() if count > 1)
    if duplicates:
        message = "review ids given more than once: " + ", ".join(repr(r) for r in duplicates)
        raise NearMissBatchRefusedError(
            message, tuple(ReviewFinding(r, "given more than once") for r in duplicates)
        )


def _sides_or_finding(
    graph: GraphHandle, review_id: str, mergeable_kinds: Collection[str]
) -> ReviewMergeSides | ReviewFinding:
    """Read one review's sides, or name why the review cannot be part of a batch."""
    from ps_service.company_merge.errors import (  # noqa: PLC0415 -- function-local keeps graph_cleanup off Company Merge at import
        StalePendingReviewError,
        UnknownReviewKindError,
    )
    from ps_service.company_merge.pending_review import (  # noqa: PLC0415 -- function-local, as above
        read_review_merge_sides,
    )

    try:
        sides = read_review_merge_sides(graph, review_id)
    except StalePendingReviewError:
        return ReviewFinding(review_id, _STALE_REVIEW)
    except UnknownReviewKindError as exc:
        return ReviewFinding(review_id, str(exc))
    if sides is None:
        return ReviewFinding(review_id, _UNKNOWN_REVIEW)
    if sides.kind not in mergeable_kinds:
        return ReviewFinding(review_id, _NO_MERGE.format(kind=sides.kind))
    return sides


def _overlap_findings(sides: Sequence[ReviewMergeSides]) -> dict[str, ReviewFinding]:
    """Name, for each review that shares a node with another selected review, every conflict.

    Returns:
        The findings keyed by review id; a review that shares nothing has none.
    """
    sharing: dict[str, list[str]] = defaultdict(list)
    for side in sides:
        for node_id in {side.incoming_id, side.existing_id}:
            sharing[node_id].append(side.review_id)
    conflicts: dict[str, list[str]] = defaultdict(list)
    for node_id, review_ids in sharing.items():
        if len(review_ids) > 1:
            for review_id in review_ids:
                others = ", ".join(repr(other) for other in review_ids if other != review_id)
                conflicts[review_id].append(f"shares {node_id!r} with review {others}")
    return {
        review_id: ReviewFinding(review_id, "overlaps another selected review: " + "; ".join(texts))
        for review_id, texts in conflicts.items()
    }


def _read_sides(
    graph: GraphHandle, review_ids: Sequence[str], mergeable_kinds: Collection[str]
) -> list[ReviewMergeSides]:
    """Read every selected review's sides, refusing the whole request if any review is unusable.

    Reviews that cannot be read are named with their state, and reviews that share a node with
    another selected review are named with every conflict, all in one refusal.
    """
    reads = [_sides_or_finding(graph, review_id, mergeable_kinds) for review_id in review_ids]
    usable = [read for read in reads if not isinstance(read, ReviewFinding)]
    overlaps = _overlap_findings(usable)
    findings = [
        read if isinstance(read, ReviewFinding) else overlaps[read.review_id]
        for read in reads
        if isinstance(read, ReviewFinding) or read.review_id in overlaps
    ]
    if findings:
        raise _refusal(findings)
    return usable


def _needs_acknowledgment(entry: NearMissMergePreviewEntry) -> AcknowledgmentNeeded | None:
    """The Policy a review's merge changes the governance of, or None when it changes none."""
    preview = entry.preview
    if not isinstance(preview, CapabilityMergePreview):
        return None
    governance = preview.governance
    if governance is None or not governance.acknowledgment_required:
        return None
    policy = governance.policy
    return AcknowledgmentNeeded(entry.review_id, policy.id, policy.title, policy.status)


def _acknowledgment_misuse(
    entries: Sequence[NearMissMergePreviewEntry], acknowledged: Collection[str]
) -> list[ReviewFinding]:
    """Name each acknowledged review id that is not selected or changes no governance."""
    by_review = {entry.review_id: entry for entry in entries}
    findings: list[ReviewFinding] = []
    for review_id in sorted(acknowledged):
        entry = by_review.get(review_id)
        if entry is None:
            findings.append(ReviewFinding(review_id, _ACK_NOT_SELECTED))
        elif not isinstance(entry.preview, CapabilityMergePreview):
            findings.append(ReviewFinding(review_id, _ACK_ON_ROLE.format(kind=entry.kind)))
        elif _needs_acknowledgment(entry) is None:
            findings.append(ReviewFinding(review_id, _ACK_NOT_NEEDED))
    return findings


def _build_all(
    graph: GraphHandle, sides: Sequence[ReviewMergeSides], acknowledged: Collection[str]
) -> list[tuple[NearMissMergePreviewEntry, NearMissMergeItem]]:
    """Preview every review and check the acknowledgments, refusing the whole request on any fault.

    Raises:
        NearMissBatchRefusedError: The planner refuses a review, or an acknowledgment is given for
            a review that is not selected, is a Role review or changes no governance.
        NearMissBatchAcknowledgmentRequiredError: A review changes governance and is not
            acknowledged.
    """
    built: list[tuple[NearMissMergePreviewEntry, NearMissMergeItem]] = []
    findings: list[ReviewFinding] = []
    for side in sides:
        try:
            built.append(_entry_and_item(graph, side, acknowledged=side.review_id in acknowledged))
        except GraphCleanupValidationError as exc:
            findings.append(ReviewFinding(side.review_id, str(exc)))
    entries = [entry for entry, _ in built]
    findings.extend(_acknowledgment_misuse(entries, acknowledged))
    if findings:
        raise _refusal(findings)
    missing = tuple(
        needed
        for entry in entries
        if not entry.acknowledged and (needed := _needs_acknowledgment(entry)) is not None
    )
    if missing:
        raise _acknowledgment_required(missing, entries)
    return built


def _acknowledgment_required(
    missing: Sequence[AcknowledgmentNeeded], entries: Sequence[NearMissMergePreviewEntry]
) -> NearMissBatchAcknowledgmentRequiredError:
    """The request for the acknowledgments a batch still lacks."""
    lines = "\n".join(
        f"- {needed.review_id!r}: changes what policy {needed.policy_title!r} ({needed.policy_id}, "
        f"{needed.policy_status}) governs"
        for needed in missing
    )
    message = (
        "these merges change what a policy governs and need your acknowledgment:\n"
        f"{lines}\nRepeat the call with their review ids in acknowledged_review_ids to receive "
        "the approval link."
    )
    return NearMissBatchAcknowledgmentRequiredError(message, tuple(missing), tuple(entries))


def _previewers() -> dict[str, Callable[..., CapabilityMergePreview | RoleMergePreview]]:
    """The merge preview of each kind a batch can merge, keyed by the review's kind label."""
    from ps_service.company_merge.reviewable_kinds import (  # noqa: PLC0415 -- function-local, as above
        CAPABILITY,
        ROLE,
    )

    return {CAPABILITY.label: preview_capability_merge, ROLE.label: preview_role_merge}


def _entry_and_item(
    graph: GraphHandle, sides: ReviewMergeSides, *, acknowledged: bool
) -> tuple[NearMissMergePreviewEntry, NearMissMergeItem]:
    """Pick the survivor of one review and preview its merge under the review's own kind."""
    from ps_service.company_merge.pending_review import (  # noqa: PLC0415 -- function-local, as above
        choose_survivor,
    )

    survivor_id, absorbed_id = choose_survivor(sides)
    preview = _previewers()[sides.kind](graph, survivor_id=survivor_id, absorbed_id=absorbed_id)
    entry = NearMissMergePreviewEntry(
        review_id=sides.review_id, kind=sides.kind, acknowledged=acknowledged, preview=preview
    )
    item = NearMissMergeItem(
        review_id=sides.review_id,
        kind=sides.kind,
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        state_digest=preview.state_digest,
        acknowledged=acknowledged,
    )
    return entry, item


def create_near_miss_merge_batch(
    graph: GraphHandle,
    *,
    review_ids: Sequence[str],
    acknowledged_review_ids: Sequence[str],
    actor: tuple[str, str],
    base_url: str,
    store: PendingApprovalStore,
    emitter: LogEmitter | None = None,
) -> NearMissBatchApproval:
    """Preview merging the selected near-miss reviews and create the one approval that binds them.

    Args:
        graph: Handle on the single-tenant graph, read only.
        review_ids: Ids of the reviews to merge, in the order the officer chose them.
        acknowledged_review_ids: Ids of the selected reviews whose governance change the officer
            accepts.
        actor: `(subject, issuer)` of the requesting officer; the approval is bound to them.
        base_url: Origin used to build the approval link, without a trailing slash.
        store: Pending-approval store that holds the approval row.
        emitter: Optional log emitter; None uses the default.

    Returns:
        One preview entry per review, with the approval id, signing URL and expiry.

    Raises:
        NearMissBatchRefusedError: The selection is empty or repeats a review, a selected review
            does not exist, is stale, or is of a kind with no merge, two selected reviews share a
            node, or the planner refuses a pair (a non-active side, two governing Policies); every
            offending review is named with its state and no approval is created. An
            acknowledgment is refused when its review is not selected, is a Role review or makes
            no governance change.
        NearMissBatchAcknowledgmentRequiredError: A selected Capability merge changes what a Policy
            governs and its review id is not in `acknowledged_review_ids`; no approval is created.
        GraphCleanupPersistenceError: The graph database could not be read.

    Side Effects:
        Creates one pending approval row and emits a structured log entry. Writes nothing to the
        graph.
    """
    _check_shape(review_ids)
    acknowledged = frozenset(acknowledged_review_ids)
    built = _build_all(graph, _read_sides(graph, review_ids, _previewers()), acknowledged)
    row, code = store.create_pending_approval(
        tool_name=TOOL_MERGE_NEAR_MISSES,
        normalized_args={"merges": [item.signed_form() for _, item in built]},
        actor_subject=actor[0],
        actor_issuer=actor[1],
        display_summary={
            "kind": "near-miss-merge-batch",
            "merges": [entry.wire_form() for entry, _ in built],
        },
    )
    emit_log_entry(
        component=_COMPONENT,
        action="create_near_miss_merge_batch",
        outcome="succeeded",
        extra={"approval_id": row.id, "merge_count": len(built)},
        emitter=emitter,
    )
    return NearMissBatchApproval(
        merges=tuple(entry for entry, _ in built),
        pending_approval_id=row.id,
        approval_url=f"{base_url}/approvals/{row.id}#{code}",
        expires_at=row.expires_at.isoformat(),
    )
