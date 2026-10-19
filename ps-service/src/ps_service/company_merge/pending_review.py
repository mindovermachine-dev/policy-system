"""`PendingReview` node persistence and the near-miss review workflow.

A `PendingReview` node records a `NearMissPair` surfaced during a Company Merge dedup pass
(Capability or Policy): the incoming and existing ids and text plus the similarity score that
was computed, so listing and resolving a review never re-queries embeddings.

Public functions:
    `persist_pending_reviews`: write one node per near miss as one gateway group.
    `read_unresolved_review_pairs`: which near-miss pairs already have a review.
    `list_pending_reviews`: read the reviews a Compliance Officer can still act on.
    `get_pending_review`: read one review by id, resolvable or not.
    `resolve_review`: keep a review's pair separate (deletes the review only).
    `read_review_merge_sides`, `choose_survivor`: read a review's pair and pick the survivor of a
        merge, which the graph-cleanup `merge-capabilities` approval then performs.

Every `PendingReview` node is unresolved by construction: a resolved review is deleted, never
marked with a status, so a missing node means "not found or already resolved". Reads call
`single_tenant_graph.query(...)` directly, like `graph_reader.py`; this module issues no write
Cypher. Its writes are Graph Write Gateway groups submitted with `submit_group`.

`resolve_review` returns `None` for a missing review. That is this module's not-found signal;
the API layer translates it to `PendingReviewNotFoundError`, because `ps_service.company_merge`
never imports `ps_service.api`. A review whose incoming or existing Capability has disappeared
raises `StalePendingReviewError` from `read_review_merge_sides` instead, which the API layer
translates with a dedicated message. The review's kind (`"Capability"` or `"Policy"`) is read from
the `PendingReview` node itself, never from user input.

A merge is not written here. `read_review_merge_sides` reads the pair and `choose_survivor` decides
the survivor in Python (earlier `created_at` wins, a missing one wins, a tie goes to the incoming
node); the graph-cleanup `merge-capabilities` executor then performs the merge and deletes the
review in the same write group.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from pydantic import ValidationError

from ps_service.company_merge.errors import (
    CompanyMergePersistenceError,
    CompanyMergeValidationError,
    StalePendingReviewError,
)
from ps_service.company_merge.generated_values import SYSTEM_VALUES
from ps_service.company_merge.graph_submission import read_rows, submit_group
from ps_service.company_merge.models import PendingReviewRecord, ResolveOutcome
from ps_service.graph_gateway.models import DeleteNode, UpsertNode
from ps_service.logging import emit_log_entry

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.generated_values import ValueSource
    from ps_service.company_merge.graph_submission import GraphWriteTarget
    from ps_service.company_merge.models import NearMissPair
    from ps_service.logging import LogEmitter

__all__ = [
    "ReviewMergeSides",
    "choose_survivor",
    "get_pending_review",
    "list_pending_reviews",
    "persist_pending_reviews",
    "read_review_merge_sides",
    "read_unresolved_review_pairs",
    "resolve_review",
]

# The two plain `MATCH`es apply the predicate `_REVIEW_MERGE_SIDES_QUERY` enforces:
# both referenced nodes still exist under the review's own `kind`, and neither is a `merged`
# tombstone. Listing a review the merge path would refuse would let it consume a passkey
# approval and fail as stale at sign time. That failure makes no graph change, so the review
# would stay listed and every retry would fail the same way.
#
# A plain (not OPTIONAL) `MATCH` does the filtering: a review whose node is gone yields no row.
# The label cannot be a static `:{kind}` because one query spans both kinds, so it is tested
# with `r.kind IN labels(a)`. `coalesce(status, 'active')` covers a node with no `status`.
_LIST_PENDING_REVIEWS_QUERY = (
    "MATCH (r:PendingReview) "
    "MATCH (a) WHERE a.id = r.incoming_id AND r.kind IN labels(a) "
    "AND coalesce(a.status, 'active') <> 'merged' "
    "MATCH (b) WHERE b.id = r.nearest_existing_id AND r.kind IN labels(b) "
    "AND coalesce(b.status, 'active') <> 'merged' "
    "RETURN r.id, r.kind, r.incoming_id, r.incoming_text, "
    "r.nearest_existing_id, r.nearest_existing_text, r.similarity, r.created_at "
    "ORDER BY r.created_at ASC"
)

# The pairs among `$pairs` that already have a review of `$kind`. No status filter: every
# `PendingReview` node is unresolved, because a resolved review is deleted. Values travel as
# params; the text is fixed.
_UNRESOLVED_PAIRS_QUERY = (
    "UNWIND $pairs AS p "
    "MATCH (r:PendingReview {kind: $kind, incoming_id: p.incoming_id, "
    "nearest_existing_id: p.nearest_existing_id}) "
    "RETURN DISTINCT r.incoming_id, r.nearest_existing_id"
)

# One review by id for audit emission. Unlike `_LIST_PENDING_REVIEWS_QUERY` it has NO
# both-nodes-exist predicate, so a stale review still yields its entity ids and the attempted
# resolution can be audited (and fail as stale) rather than being invisible.
_GET_REVIEW_QUERY = (
    "MATCH (r:PendingReview {id: $review_id}) "
    "RETURN r.id, r.kind, r.incoming_id, r.incoming_text, "
    "r.nearest_existing_id, r.nearest_existing_text, r.similarity, r.created_at"
)

_FIND_REVIEW_QUERY = "MATCH (r:PendingReview {id: $review_id}) RETURN r.kind"

# One read of a review's kind and its two sides for the near-miss merge request: the review's own
# ids, whether each side is still a live (non-`merged`) Capability, and each side's `created_at`.
# The label is static because only a Capability review can be merged; a review of another kind
# still yields its row (with both sides absent), so the caller can name the kind.
_REVIEW_MERGE_SIDES_QUERY = (
    "MATCH (r:PendingReview {id: $review_id}) "
    "OPTIONAL MATCH (a:Capability {id: r.incoming_id}) "
    "WHERE coalesce(a.status, 'active') <> 'merged' "
    "OPTIONAL MATCH (b:Capability {id: r.nearest_existing_id}) "
    "WHERE coalesce(b.status, 'active') <> 'merged' "
    "RETURN r.kind, r.incoming_id, r.nearest_existing_id, "
    "a IS NOT NULL, b IS NOT NULL, a.created_at, b.created_at"
)

_REVIEW_ID_PREFIX = "review_"

_COMPONENT = "company_merge"
_RESOLVE_ACTION = "resolve_near_miss_review"


def build_pending_review_primitives(
    near_misses: tuple[NearMissPair, ...],
    *,
    kind: Literal["Capability", "Policy"],
    values: ValueSource,
) -> tuple[UpsertNode, ...]:
    """Build one `PendingReview` node primitive per `NearMissPair`, ids and times fixed.

    Pure apart from `values`: nothing is read or submitted. For each pair, in order, the id is
    drawn first (`review_<values.new_id()>`) and then the instant (`values.now()` as ISO-8601
    text). Both are drawn here, while the group is built, so the group carries literals:
    submitting it twice leaves one node per review (an `UpsertNode` merges into the node it
    created) and replaying the mutation log reproduces the same ids and timestamps.

    Args:
        near_misses: Near-miss pairs to record; an empty tuple yields an empty result.
        kind: Label of the entities the pairs refer to, stored on each review.
        values: Source of new ids and the current instant.

    Returns:
        One `UpsertNode` per pair, in input order, each with label `PendingReview`.

    Raises:
        CompanyMergePersistenceError: A review carries a property the gateway cannot store, such
            as a non-finite similarity. Nothing has been submitted by then.
    """
    primitives: list[UpsertNode] = []
    for pair in near_misses:
        review_id = f"{_REVIEW_ID_PREFIX}{values.new_id()}"
        try:
            primitives.append(
                UpsertNode(
                    label="PendingReview",
                    id=review_id,
                    properties={
                        "kind": kind,
                        "status": "pending",
                        "incoming_id": pair.incoming_id,
                        "incoming_text": pair.incoming_text,
                        "nearest_existing_id": pair.nearest_existing_id,
                        "nearest_existing_text": pair.nearest_existing_text,
                        "similarity": pair.similarity,
                        "created_at": values.now().isoformat(),
                    },
                )
            )
        except ValidationError as exc:
            raise CompanyMergePersistenceError(
                f"PendingReview {review_id!r} has a property the graph cannot store "
                "(a reserved key, or a value that is not finite or is out of range)"
            ) from exc
    return tuple(primitives)


def read_unresolved_review_pairs(
    graph: GraphHandle,
    near_misses: tuple[NearMissPair, ...],
    *,
    kind: Literal["Capability", "Policy"],
) -> set[tuple[str, str]]:
    """Return the `(incoming_id, nearest_existing_id)` pairs that already have a review.

    One batched, parameterised read covers every pair. Every `PendingReview` node is unresolved
    (a resolved review is deleted), so no status filter applies. Nothing is read for an empty
    `near_misses`.

    Args:
        graph: Read handle on the single-tenant `policy_system` graph.
        near_misses: The pairs a caller is about to record.
        kind: Label of the entities the pairs refer to; only reviews of this kind match.

    Returns:
        The subset of the pairs for which a `PendingReview` of `kind` exists.

    Raises:
        CompanyMergePersistenceError: The read of `graph` failed.
    """
    if not near_misses:
        return set()
    pairs = list(
        dict.fromkeys((pair.incoming_id, pair.nearest_existing_id) for pair in near_misses)
    )
    rows = read_rows(
        graph,
        _UNRESOLVED_PAIRS_QUERY,
        {
            "kind": kind,
            "pairs": [
                {"incoming_id": incoming, "nearest_existing_id": nearest}
                for incoming, nearest in pairs
            ],
        },
    )
    return {(cast("str", row[0]), cast("str", row[1])) for row in rows}


def persist_pending_reviews(
    target: GraphWriteTarget,
    near_misses: tuple[NearMissPair, ...],
    *,
    kind: Literal["Capability", "Policy"],
    values: ValueSource = SYSTEM_VALUES,
) -> None:
    """Persist one `PendingReview` node per `NearMissPair` as one gateway group.

    Always a create: every pair received is a new occurrence surfaced during this merge run, and
    nothing is read first. A repeat `merge_baseline_graph` run over unchanged input can
    re-surface the same `incoming_id` and `nearest_existing_id` pair and create a duplicate
    review; deduplicating review records is not done here.

    A review `id` is `review_` plus a uuid. A `PendingReview` is an operational record, not a
    canonical regulatory entity, so it has no stable content to hash. The uuid and `created_at`
    come from `values` and are fixed before submission (see `build_pending_review_primitives`).

    Args:
        target: Gateway target (submitter, graph and audit event) the group goes to.
        near_misses: Near-miss pairs to record; an empty tuple submits nothing.
        kind: `"Capability"` or `"Policy"`, the label of the entities the pairs refer to.
        values: Source of new ids and the current instant; defaults to the system clock and
            uuid generator.

    Side Effects:
        Submits one mutation group through the Graph Write Gateway, creating the nodes.

    Raises:
        CompanyMergePersistenceError: A review carries a property the gateway cannot store, or
            the gateway did not take the group (`FalkorDB write failed:`).
    """
    submit_group(target, build_pending_review_primitives(near_misses, kind=kind, values=values))


def list_pending_reviews(single_tenant_graph: GraphHandle) -> tuple[PendingReviewRecord, ...]:
    """List every resolvable `PendingReview`, oldest first.

    Every `PendingReview` node is unresolved by construction, so no status filter is applied,
    although `status` is still written at persist time for anyone inspecting the graph. Unresolved
    is not the same as resolvable: only reviews whose incoming and existing nodes both still
    exist, under the review's kind and not as `merged` tombstones, are returned. This matches
    what the merge path enforces, so a Compliance Officer is never offered a review that would
    fail as stale. An excluded review is not deleted; the next similarity sweep recreates the
    candidate if it still applies.

    Args:
        single_tenant_graph: Read handle on the single-tenant `policy_system` graph.

    Returns:
        The resolvable reviews ordered by `created_at` ascending; empty when there are none.
    """
    result = single_tenant_graph.query(_LIST_PENDING_REVIEWS_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    return tuple(_record_from_row(row) for row in rows)


def get_pending_review(
    single_tenant_graph: GraphHandle, review_id: str
) -> PendingReviewRecord | None:
    """Read one `PendingReview` by id, whether or not it is still resolvable.

    Unlike `list_pending_reviews` it applies no resolvable predicate: a review whose referenced
    nodes are gone is still returned, so an attempted resolution can be audited with the entity
    ids the review names.

    Args:
        single_tenant_graph: Read handle on the single-tenant `policy_system` graph.
        review_id: Id of the review, as stored (`review_<uuid>`).

    Returns:
        The review record, or `None` when no review has that id (never existed or already
        resolved).
    """
    result = single_tenant_graph.query(_GET_REVIEW_QUERY, params={"review_id": review_id})
    rows = cast("list[list[object]]", result.result_set)
    return _record_from_row(rows[0]) if rows else None


def _record_from_row(row: list[object]) -> PendingReviewRecord:
    """Map one result row, in the column order of the list and get queries, to a record."""
    (
        review_id,
        kind,
        incoming_id,
        incoming_text,
        nearest_existing_id,
        nearest_existing_text,
        similarity,
        created_at,
    ) = row
    return PendingReviewRecord(
        id=cast("str", review_id),
        kind=cast('Literal["Capability", "Policy"]', kind),
        incoming_id=cast("str", incoming_id),
        incoming_text=cast("str", incoming_text),
        nearest_existing_id=cast("str", nearest_existing_id),
        nearest_existing_text=cast("str", nearest_existing_text),
        similarity=cast("float", similarity),
        created_at=cast("str", created_at),
    )


def resolve_review(
    single_tenant_graph: GraphHandle,
    review_id: str,
    decision: Literal["keep-separate"],
    target: GraphWriteTarget,
    *,
    emitter: LogEmitter | None = None,
) -> ResolveOutcome | None:
    """Resolve one `PendingReview` by keeping the pair separate.

    Reads the review first. No row means the id never existed or the review was already resolved
    (a resolved review is deleted, so the two cannot be told apart), and the function returns
    `None` before any write.

    Submits one gateway group on `target` whose only primitive deletes the `PendingReview` node;
    no other node or edge changes. The review is read first and deleted after, with no lock
    between: a review that vanished in between makes the delete a no-op because the gateway drops
    it.

    A merge is not decided here. It is requested through the graph-cleanup `merge-capabilities`
    approval, which reads the pair with `read_review_merge_sides` and `choose_survivor`.

    Args:
        single_tenant_graph: Read handle on the single-tenant `policy_system` graph.
        review_id: Id of the review, as returned by `list_pending_reviews`.
        decision: Must be `"keep-separate"`.
        target: Gateway target (submitter, graph and audit event) the write group goes to.
        emitter: Log sink for the resolution entry; None uses the default emitter.

    Returns:
        A `ResolveOutcome` with `winner_id` and `loser_id` None (they stay on the type for wire
        stability). None when the review does not exist or was already resolved.

    Raises:
        CompanyMergeValidationError: `decision` is anything but `"keep-separate"`. Nothing is
            read or written.
        CompanyMergePersistenceError: The gateway did not take the group.

    Side Effects:
        Submits one mutation group through the Graph Write Gateway, and on success emits one
        structured log entry: `component="company_merge"`, `action="resolve_near_miss_review"`,
        `entity_id=review_id`, `outcome=decision`.
    """
    if decision != "keep-separate":
        raise CompanyMergeValidationError(
            f"resolve_review only keeps a pair separate, not {decision!r}; "
            "a merge goes through the merge-capabilities approval"
        )
    result = single_tenant_graph.query(_FIND_REVIEW_QUERY, params={"review_id": review_id})
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return None

    submit_group(target, (DeleteNode(label="PendingReview", id=review_id),))
    emit_log_entry(
        component=_COMPONENT,
        action=_RESOLVE_ACTION,
        entity_id=review_id,
        outcome=decision,
        emitter=emitter,
    )
    return ResolveOutcome(review_id=review_id, decision=decision)


def _incoming_wins(incoming_created_at: object, existing_created_at: object) -> bool:
    """Decide whether the incoming node wins, as `coalesce(a, '') <= coalesce(b, '')`.

    ISO-8601 text compares as text. A missing `created_at` counts as `''`, which sorts first, so
    it wins over any present one; equal or both missing goes to the incoming node. A stored value
    that is not text cannot be compared with text, so the existing node wins.
    """
    incoming = "" if incoming_created_at is None else incoming_created_at
    existing = "" if existing_created_at is None else existing_created_at
    if not isinstance(incoming, str) or not isinstance(existing, str):
        return False
    return incoming <= existing


@dataclass(frozen=True, slots=True)
class ReviewMergeSides:
    """The two sides of a near-miss review, as `read_review_merge_sides` read them.

    Attributes:
        review_id: Id of the review.
        kind: The review's kind, `"Capability"` or `"Policy"`.
        incoming_id: Id of the node whose arrival raised the near miss.
        existing_id: Id of the nearest existing node.
        incoming_created_at: ISO-8601 `created_at` of the incoming node, or None when it has none.
        existing_created_at: ISO-8601 `created_at` of the existing node, or None when it has none.
    """

    review_id: str
    kind: Literal["Capability", "Policy"]
    incoming_id: str
    existing_id: str
    incoming_created_at: object
    existing_created_at: object


def read_review_merge_sides(
    single_tenant_graph: GraphHandle, review_id: str
) -> ReviewMergeSides | None:
    """Read one review's kind, both node ids and both `created_at` stamps; write nothing.

    Args:
        single_tenant_graph: Read handle on the single-tenant graph.
        review_id: Id of the review, as returned by `list_pending_reviews`.

    Returns:
        The review's sides, or None when no unresolved review has that id.

    Raises:
        StalePendingReviewError: A Capability review whose incoming or existing node no longer
            exists or is a `merged` tombstone. Nothing is changed.
    """
    result = single_tenant_graph.query(_REVIEW_MERGE_SIDES_QUERY, params={"review_id": review_id})
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return None
    kind, incoming_id, existing_id, incoming_live, existing_live, incoming_at, existing_at = rows[0]
    typed_kind = cast('Literal["Capability", "Policy"]', kind)
    if typed_kind == "Capability" and not (incoming_live and existing_live):
        raise StalePendingReviewError(review_id)
    return ReviewMergeSides(
        review_id=review_id,
        kind=typed_kind,
        incoming_id=cast("str", incoming_id),
        existing_id=cast("str", existing_id),
        incoming_created_at=incoming_at,
        existing_created_at=existing_at,
    )


def choose_survivor(sides: ReviewMergeSides) -> tuple[str, str]:
    """Pick the survivor and the absorbed node of a near-miss merge.

    The node with the earlier `created_at` survives; a missing `created_at` counts as earliest and
    a tie (or both missing) goes to the incoming node.

    Args:
        sides: The review's two sides.

    Returns:
        `(survivor_id, absorbed_id)`.
    """
    if _incoming_wins(sides.incoming_created_at, sides.existing_created_at):
        return sides.incoming_id, sides.existing_id
    return sides.existing_id, sides.incoming_id
