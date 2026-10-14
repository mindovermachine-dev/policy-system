"""`PendingReview` node persistence and the near-miss review workflow.

A `PendingReview` node records a `NearMissPair` surfaced during a Company Merge dedup pass
(Capability or Policy): the incoming and existing ids and text plus the similarity score that
was computed, so listing and resolving a review never re-queries embeddings.

Public functions:
    `persist_pending_reviews`: write one node per near miss as one gateway group.
    `list_pending_reviews`: read the reviews a Compliance Officer can still act on.
    `get_pending_review`: read one review by id, resolvable or not.
    `resolve_review`: decide a review, either `keep-separate` or `merge`.

Every `PendingReview` node is unresolved by construction: a resolved review is deleted, never
marked with a status, so a missing node means "not found or already resolved". Reads call
`single_tenant_graph.query(...)` directly, like `graph_reader.py`; this module issues no write
Cypher. Its writes are Graph Write Gateway groups submitted with `submit_group`.

`resolve_review` returns `None` for a missing review. That is this module's not-found signal;
the API layer translates it to `PendingReviewNotFoundError`, because `ps_service.company_merge`
never imports `ps_service.api`. A review whose incoming or existing node has disappeared raises
`StalePendingReviewError` instead, which the API layer translates with a dedicated message. The
merge branch checks both nodes in one existence read before it submits anything. Both query
templates are `{kind}`-templated (`"Capability"` or `"Policy"`, read from the `PendingReview`
node itself, never from user input) because Cypher labels are static and cannot be a runtime
property reference.

A merge is a read-resolve-write sequence. The winner is decided in Python (earlier `created_at`
wins, a missing one wins, a tie goes to the incoming node). The loser's edges are read with
`RETURN DISTINCT`, and one group rewires them onto the winner and deletes the loser and the
review. Several `Obligation`s may `REQUIRE` the same loser `Capability`; a repeated edge is one
primitive and the loser is deleted once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from pydantic import ValidationError

from ps_service.company_merge.errors import CompanyMergePersistenceError, StalePendingReviewError
from ps_service.company_merge.generated_values import SYSTEM_VALUES
from ps_service.company_merge.graph_submission import edge_identity, read_rows, submit_group
from ps_service.company_merge.models import PendingReviewRecord, ResolveOutcome
from ps_service.graph_gateway.models import DeleteNode, NodeRef, UpsertEdge, UpsertNode
from ps_service.logging import emit_log_entry

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.generated_values import ValueSource
    from ps_service.company_merge.graph_submission import GraphWriteTarget
    from ps_service.company_merge.models import NearMissPair
    from ps_service.logging import LogEmitter

__all__ = [
    "get_pending_review",
    "list_pending_reviews",
    "persist_pending_reviews",
    "resolve_review",
]

# The two plain `MATCH`es apply the predicate `_MERGE_EXISTENCE_CHECK_QUERY_TEMPLATE` enforces:
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

# One review by id for audit emission. Unlike `_LIST_PENDING_REVIEWS_QUERY` it has NO
# both-nodes-exist predicate, so a stale review still yields its entity ids and the attempted
# resolution can be audited (and fail as stale) rather than being invisible.
_GET_REVIEW_QUERY = (
    "MATCH (r:PendingReview {id: $review_id}) "
    "RETURN r.id, r.kind, r.incoming_id, r.incoming_text, "
    "r.nearest_existing_id, r.nearest_existing_text, r.similarity, r.created_at"
)

_FIND_REVIEW_QUERY = "MATCH (r:PendingReview {id: $review_id}) RETURN r.kind"

# A `merged` Capability tombstone is never a near-miss merge side: the existence check treats it
# as gone (so a stale review naming one raises `StalePendingReviewError` before any write) and
# the merge write re-points every inbound `MERGED_INTO` edge from the deleted loser onto the
# winner, so no tombstone is left redirecting at a deleted node. For kind Policy neither
# predicate nor pattern can match, since no Policy is `merged`.
#
# One combined existence-check read, issued only for decision="merge" after `_FIND_REVIEW_QUERY`
# confirmed the review exists. `{kind}` is templated because Cypher labels are static;
# `r.incoming_id` and `r.nearest_existing_id` are property references off the re-matched `r`
# (id-indexed), not params, so it stays a single query.
_MERGE_EXISTENCE_CHECK_QUERY_TEMPLATE = (
    "MATCH (r:PendingReview {{id: $review_id}}) "
    "OPTIONAL MATCH (a:{kind} {{id: r.incoming_id}}) "
    "WHERE coalesce(a.status, 'active') <> 'merged' "
    "OPTIONAL MATCH (b:{kind} {{id: r.nearest_existing_id}}) "
    "WHERE coalesce(b.status, 'active') <> 'merged' "
    "RETURN r.incoming_id AS incoming_id, r.nearest_existing_id AS nearest_existing_id, "
    "a IS NOT NULL AS incoming_exists, b IS NOT NULL AS existing_exists, "
    "a.created_at AS incoming_created_at, b.created_at AS existing_created_at"
)

# The loser's edges are READ, then rewired by gateway primitives. One fixed read per edge family
# (inbound REQUIRES, outbound GOVERNED_BY, inbound GOVERNED_BY, outbound SUPPORTED_BY, inbound
# MERGED_INTO); `{kind}` is the loser's label (labels are static in Cypher) and the loser's id is
# a param. Every family is read whatever the kind: a pattern that cannot exist for the kind
# returns no rows. `DISTINCT` guards against row multiplication (several Obligations can require
# one loser; a parallel edge is one edge).
#
# (edge type, other end's label, loser is the target, read template)
_LOSER_EDGE_READS: tuple[tuple[str, str, bool, str], ...] = (
    (
        "REQUIRES",
        "Obligation",
        True,
        "MATCH (n:Obligation)-[:REQUIRES]->(l:{kind} {{id: $loser_id}}) RETURN DISTINCT n.id",
    ),
    (
        "GOVERNED_BY",
        "Policy",
        False,
        "MATCH (l:{kind} {{id: $loser_id}})-[:GOVERNED_BY]->(n:Policy) RETURN DISTINCT n.id",
    ),
    (
        "GOVERNED_BY",
        "Capability",
        True,
        "MATCH (n:Capability)-[:GOVERNED_BY]->(l:{kind} {{id: $loser_id}}) RETURN DISTINCT n.id",
    ),
    (
        "SUPPORTED_BY",
        "Standard",
        False,
        "MATCH (l:{kind} {{id: $loser_id}})-[:SUPPORTED_BY]->(n:Standard) RETURN DISTINCT n.id",
    ),
    (
        "MERGED_INTO",
        "Capability",
        True,
        "MATCH (n:Capability)-[:MERGED_INTO]->(l:{kind} {{id: $loser_id}}) RETURN DISTINCT n.id",
    ),
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
    decision: Literal["merge", "keep-separate"],
    target: GraphWriteTarget,
    *,
    emitter: LogEmitter | None = None,
) -> ResolveOutcome | None:
    """Resolve one `PendingReview`, either keeping the pair separate or merging it.

    Reads the review first. No row means the id never existed or the review was already resolved
    (a resolved review is deleted, so the two cannot be told apart), and the function returns
    `None` before any write.

    `decision="keep-separate"` submits one gateway group on `target` whose only primitive
    deletes the `PendingReview` node; no other node or edge changes. The review is read first
    and deleted after, with no lock between: a review that vanished in between makes the delete
    a no-op because the gateway drops it.

    `decision="merge"` delegates to `_resolve_merge`: one existence read of both referenced
    nodes, then, only if both still exist, one atomic group that rewires the loser's edges onto
    the winner and deletes the loser and the review. All of it happens before the log entry is
    emitted.

    Args:
        single_tenant_graph: Read handle on the single-tenant `policy_system` graph.
        review_id: Id of the review, as returned by `list_pending_reviews`.
        decision: `"keep-separate"` to delete only the review, or `"merge"` to merge the pair.
        target: Gateway target (submitter, graph and audit event) the write group goes to.
        emitter: Log sink for the resolution entry; None uses the default emitter.

    Returns:
        A `ResolveOutcome`. For `"merge"` it carries `winner_id` and `loser_id`; for
        `"keep-separate"` both are None. None when the review does not exist, was already
        resolved, or vanished between the first read and the existence check.

    Raises:
        StalePendingReviewError: `decision="merge"` and the incoming or existing node no longer
            exists or is a `merged` tombstone. Nothing has been written.
        CompanyMergePersistenceError: The gateway did not take the group.

    Side Effects:
        Submits one mutation group through the Graph Write Gateway, and on success emits one
        structured log entry: `component="company_merge"`, `action="resolve_near_miss_review"`,
        `entity_id=review_id`, `outcome=decision`. A merge also carries `winner_id` and
        `loser_id` in `extra`.

    Business Rules:
        The winner of a merge is the node with the earlier `created_at`; a missing `created_at`
        counts as earliest, and a tie goes to the incoming node.
    """
    result = single_tenant_graph.query(_FIND_REVIEW_QUERY, params={"review_id": review_id})
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return None
    kind = cast('Literal["Capability", "Policy"]', rows[0][0])

    if decision == "merge":
        merge_ids = _resolve_merge(single_tenant_graph, review_id, kind, target)
        if merge_ids is None:
            return None
        winner_id, loser_id = merge_ids
        emit_log_entry(
            component=_COMPONENT,
            action=_RESOLVE_ACTION,
            entity_id=review_id,
            outcome=decision,
            extra={"winner_id": winner_id, "loser_id": loser_id},
            emitter=emitter,
        )
        return ResolveOutcome(
            review_id=review_id, decision=decision, winner_id=winner_id, loser_id=loser_id
        )

    submit_group(target, (DeleteNode(label="PendingReview", id=review_id),))
    emit_log_entry(
        component=_COMPONENT,
        action=_RESOLVE_ACTION,
        entity_id=review_id,
        outcome=decision,
        emitter=emitter,
    )
    return ResolveOutcome(review_id=review_id, decision=decision)


def _resolve_merge(
    single_tenant_graph: GraphHandle,
    review_id: str,
    kind: Literal["Capability", "Policy"],
    target: GraphWriteTarget,
) -> tuple[str, str] | None:
    """Run the `decision="merge"` half of `resolve_review`: read, pick a winner, write.

    Reads both referenced nodes in one existence check that also returns their `created_at`.
    Zero rows (the review vanished after `resolve_review` read it, a narrow race) returns `None`,
    which the caller treats as not found. A row with either node missing raises
    `StalePendingReviewError` before anything is submitted.

    The winner is decided in Python by `_incoming_wins`, then the loser's five edge families are
    read (`_LOSER_EDGE_READS`) and submitted as one group on `target`, all or nothing: an
    `UpsertEdge` putting each loser edge on the winner, `DeleteNode(loser)` (which removes any
    remaining edges), then `DeleteNode(PendingReview)`. No lock spans the reads and the write: a
    node deleted in between makes the gateway refuse the group on a missing endpoint, and a
    review deleted in between makes its delete a no-op.

    Args:
        single_tenant_graph: Read handle on the single-tenant graph.
        review_id: Id of the review being resolved.
        kind: `"Capability"` or `"Policy"`, read from the review.
        target: Gateway target the write group goes to.

    Returns:
        `(winner_id, loser_id)`, or None when the review no longer exists.

    Raises:
        StalePendingReviewError: The incoming or existing node no longer exists.
        CompanyMergePersistenceError: The gateway did not take the group.
    """
    existence_query = _MERGE_EXISTENCE_CHECK_QUERY_TEMPLATE.format(kind=kind)
    existence_result = single_tenant_graph.query(existence_query, params={"review_id": review_id})
    existence_rows = cast("list[list[object]]", existence_result.result_set)
    if not existence_rows:
        return None
    (
        incoming_id,
        nearest_existing_id,
        incoming_exists,
        existing_exists,
        incoming_created_at,
        existing_created_at,
    ) = existence_rows[0]
    if not incoming_exists or not existing_exists:
        raise StalePendingReviewError(review_id)

    incoming_id, nearest_existing_id = cast("str", incoming_id), cast("str", nearest_existing_id)
    if _incoming_wins(incoming_created_at, existing_created_at):
        winner_id, loser_id = incoming_id, nearest_existing_id
    else:
        winner_id, loser_id = nearest_existing_id, incoming_id

    primitives = (
        *_rewire_primitives(single_tenant_graph, kind, winner_id, loser_id),
        DeleteNode(label=kind, id=loser_id),
        DeleteNode(label="PendingReview", id=review_id),
    )
    submit_group(target, primitives)
    return winner_id, loser_id


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


def _rewire_primitives(
    single_tenant_graph: GraphHandle,
    kind: Literal["Capability", "Policy"],
    winner_id: str,
    loser_id: str,
) -> tuple[UpsertEdge, ...]:
    """Read the loser's edges and build one `UpsertEdge` per edge, pointed at the winner.

    The other end of each edge was just read from the graph, and the winner exists (the
    existence check), so both endpoints exist. An edge with the same identity from two families
    (a self-loop) folds to one. Edges carry no properties.
    """
    edges: dict[str, UpsertEdge] = {}
    for edge_type, other_label, loser_is_target, template in _LOSER_EDGE_READS:
        rows = read_rows(
            single_tenant_graph, template.format(kind=kind), params={"loser_id": loser_id}
        )
        for (other_id,) in rows:
            other = NodeRef(label=other_label, id=cast("str", other_id))
            winner = NodeRef(label=kind, id=winner_id)
            source, edge_target = (other, winner) if loser_is_target else (winner, other)
            identity = edge_identity(edge_type, source.id, edge_target.id)
            edges.setdefault(
                identity,
                UpsertEdge(type=edge_type, identity=identity, source=source, target=edge_target),
            )
    return tuple(edges.values())
