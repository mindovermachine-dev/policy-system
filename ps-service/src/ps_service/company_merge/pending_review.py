"""ps_service.company_merge.pending_review -- `PendingReview` node persistence.

Issue #35 (near-miss review workflow), Slice 1 (AC-BI-001, AC-BI-002):
`persist_pending_reviews` writes one `PendingReview` node per `NearMissPair`
surfaced during a Company Merge dedup pass (Capability or Policy), preserving
the pair's incoming/existing ids and text plus the similarity score that was
actually computed -- so a later `near-misses list`/`resolve` workflow
(Slices 2-4, not yet implemented) never needs to re-query embeddings to
render or act on it (PLAN.md §2.1/§4.1).

Slice 2 (AC-BI-003) adds `list_pending_reviews`: every `PendingReview` node
IS unresolved by construction (a resolved one is deleted outright, never
soft-status-changed, PLAN.md §2.1/§4.2) -- so no `WHERE status = 'pending'`
filter is needed. It does filter on *resolvability* since issue #196
(Cause 8): see that function's own docstring.
This is a read, so -- mirroring `graph_reader.py`'s own
"every query here is read-only, no dependency-health wrapper" convention --
it calls `single_tenant_graph.query(...)` directly. Since issue #210 this module issues no
write Cypher at all: its writes are gateway groups (`submit_group`).

Slice 3 (AC-BI-004, the not-found half of AC-BI-008, the `keep-separate`
half of AC-BI-009) adds `resolve_review`: reads the review first (a plain
read, like `list_pending_reviews`) and returns `None` immediately -- before
any write -- when `review_id` doesn't exist or was already resolved (§2.1:
a resolved review's node is deleted outright, so the two conditions
collapse to one). `None` is this module's own not-found signal, translated
to the API-boundary `PendingReviewNotFoundError` by
`api.near_miss_review_orchestration.run_resolve_near_miss`, not raised
here: `ps_service.company_merge` never imports from `ps_service.api` (M6
layering is one-directional -- `api` imports `company_merge`
function-locally, never the reverse), mirroring `RestoreArtifactRejectedError`'s
own "API-boundary translation of a lower-layer condition" pattern
(`ps_service/api/errors.py`).

Slice 4 (AC-BI-005/006/007, AC-BI-008/009 completed) widens `resolve_review`
to add `decision="merge"`. Per CHANGES.md H2, the merge branch runs one more
read after the shared "does the review exist" check -- a combined
existence-check (`_MERGE_EXISTENCE_CHECK_QUERY_TEMPLATE`) confirming BOTH
`incoming_id` and `nearest_existing_id` still resolve to real nodes -- before
the atomic merge group (issue #210; CHANGES.md Appendix C1 before it) is
ever submitted. A stale reference (either id no longer resolves -- e.g. a prior,
unrelated merge already deleted it) raises `StalePendingReviewError`
(`company_merge/errors.py`), this module's own not-found-adjacent signal for
that specific case, translated to `PendingReviewNotFoundError` with a
dedicated message at the API boundary exactly like the plain `None` case
above. Both query templates are `{kind}`-templated (`"Capability"` or
`"Policy"`, read from the `PendingReview` node itself, never
adapter/user-sourced) since Cypher labels are static/compile-time --
`{kind}` cannot be a runtime property reference, unlike the ids themselves
(mirrors `dedup.py:101-103`'s own "fixed literal, no allow-list needed"
precedent).

The merge is a read-resolve-write sequence (issue #210): the winner is decided in Python (older
`created_at` wins, a missing one wins, a tie goes to the incoming node), the loser's edges are read
with `RETURN DISTINCT`, and one group rewires them onto the winner and deletes the loser and the
review. Several `Obligation`s `REQUIRES`-ing the same loser `Capability` is the domain's primary
scenario (`docs/artifacts/ps-domain-concepts.md`): a repeated edge is one primitive and the loser
is deleted once. The pre-#210 single-statement implementation is kept, frozen, in
`tests/company_merge/_legacy_resolve_reference.py` as the equivalence reference; the
`falkordb_live` test in `test_pending_review_resolve.py` is the live anchor.
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

# Issue #196 (Cause 8): the two `MATCH`es apply the same predicate
# `_MERGE_EXISTENCE_CHECK_QUERY_TEMPLATE` below enforces -- both referenced
# nodes still exist, under the review's own `kind`, and neither is a `merged`
# tombstone. Without them this query reported every `PendingReview` node
# unconditionally, so a review the merge path would refuse was still offered:
# it consumed a passkey approval, raised `StalePendingReviewError` at sign
# time and -- since that path deliberately makes no graph changes (H2) -- was
# never retired, staying listed forever while every retry failed identically.
#
# Plain (not OPTIONAL) `MATCH` is what performs the filtering: a review whose
# node is gone simply produces no row. The label cannot be a static `:{kind}`
# here the way it can in the merge check, because one query spans both kinds,
# so the label is tested with `r.kind IN labels(a)` instead -- verified
# against a real FalkorDB, along with the `coalesce(status, 'active')` default
# for a node carrying no `status` property at all.
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

# Issue #195: one review by id for audit emission. Unlike `_LIST_PENDING_REVIEWS_QUERY` it has NO
# both-nodes-exist predicate, so a stale review still yields its entity ids and the attempted
# resolution can be audited (and fail as stale) rather than being invisible.
_GET_REVIEW_QUERY = (
    "MATCH (r:PendingReview {id: $review_id}) "
    "RETURN r.id, r.kind, r.incoming_id, r.incoming_text, "
    "r.nearest_existing_id, r.nearest_existing_text, r.similarity, r.created_at"
)

_FIND_REVIEW_QUERY = "MATCH (r:PendingReview {id: $review_id}) RETURN r.kind"

# Issue #190 -- a `merged` Capability tombstone is never a near-miss merge
# side: the existence check treats it as gone (so a stale review naming one
# is `StalePendingReviewError` before any write) and the merge write
# re-points every inbound `MERGED_INTO` edge from the deleted loser onto the
# winner, so no tombstone is left redirecting at a deleted node. For kind
# Policy neither predicate nor pattern can match (no Policy is `merged`).
#
# CHANGES.md H2 -- one combined existence-check read, issued only for
# decision="merge", after the shared _FIND_REVIEW_QUERY above already
# confirmed the review itself exists. `{kind}` is templated (labels are
# static/compile-time in Cypher); `r.incoming_id`/`r.nearest_existing_id`
# are runtime property references off the freshly re-matched `r` (cheap,
# id-indexed), not params -- so this stays a single query per call, not two.
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

# Issue #210: the loser's edges are READ, then rewired by gateway primitives. One fixed read per
# edge family, in the order the old single statement rewired them (inbound REQUIRES, outbound
# GOVERNED_BY, inbound GOVERNED_BY, outbound SUPPORTED_BY, inbound MERGED_INTO); `{kind}` is the
# loser's label (labels are static in Cypher) and the loser's id is a param. Every family is read
# whatever the kind, as the old statement matched every family: a pattern that cannot exist for
# the kind returns no rows. `DISTINCT` is the row-multiplication guard the old `WITH DISTINCT
# winner, loser` was (several Obligations can require one loser; a parallel edge is one edge).
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
    """One `PendingReview` node primitive per `NearMissPair`, its uuid and `created_at` fixed.

    Pure apart from `values`: nothing is read or submitted. For each pair, in order, the id is
    drawn first (`review_<values.new_id()>`) and then the instant (`values.now()` as ISO-8601
    text), the order the Cypher writer drew them (`uuid4`, then `datetime.now`). They are drawn
    HERE, while the group is built, so the group carries literals: submitting it twice leaves one
    node per review (an `UpsertNode` merges into the node it created) and a replay of the log
    reproduces the same ids and timestamps (AC-BI-007).

    Raises:
        CompanyMergePersistenceError: a review carries a property the gateway cannot store (a
            non-finite similarity); nothing has been submitted by then.
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
    """Persist one `PendingReview` node per `NearMissPair`, as ONE gateway group (AC-BI-001/002).

    Always a create: every `NearMissPair` this function receives is, by construction, a genuinely
    new occurrence surfaced during THIS merge run -- there is no dedup-of-near-misses concept in
    this schema (`dedup.py` never re-surfaces a pair it already recorded in a prior run). A repeat
    `merge_baseline_graph` run over unchanged input could in principle re-surface the same
    `incoming_id`/`nearest_existing_id` pair and create a duplicate `PendingReview` -- an accepted,
    pre-existing-pattern-consistent gap (PLAN.md §4.1): deduplicating review records themselves is
    out of this issue's AC scope (no bulk/batch resolve). Nothing is read first, for that reason.

    `id` is `review_` plus a uuid, prefixed for operator readability -- a `PendingReview` is an
    operational/review record, not a regulatory/canonical entity, so it has no stable content to
    hash (PLAN.md §2.4). Since issue #210 the uuid and `created_at` come from `values` and are
    fixed before submission (`build_pending_review_primitives`). No near-miss submits nothing.

    Raises:
        CompanyMergePersistenceError: a review carries a property the gateway cannot store, or the
            gateway did not take the group (`FalkorDB write failed:`).
    """
    submit_group(target, build_pending_review_primitives(near_misses, kind=kind, values=values))


def list_pending_reviews(single_tenant_graph: GraphHandle) -> tuple[PendingReviewRecord, ...]:
    """Every *resolvable* unresolved `PendingReview`, in `created_at` order (AC-BI-003).

    Every `PendingReview` node IS unresolved by construction (§2.1: a
    resolved one is deleted outright, never soft-status-changed) -- no
    `WHERE status = 'pending'` filter is needed, though the `status`
    property is still written at persist time (forward-compatibility /
    observability for an operator inspecting the graph directly).

    Unresolved is not the same as resolvable, though: issue #196 (Cause 8)
    added the same both-nodes-exist/neither-is-a-`merged`-tombstone predicate
    the merge path enforces, because a review only this read accepted was
    offered to a Compliance Officer, consumed a passkey approval, then failed
    as stale at sign time without ever being retired -- listed forever, with
    every retry failing identically. A review excluded here is not deleted;
    the next similarity sweep recreates the candidate if it still applies.
    """
    result = single_tenant_graph.query(_LIST_PENDING_REVIEWS_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    return tuple(_record_from_row(row) for row in rows)


def get_pending_review(
    single_tenant_graph: GraphHandle, review_id: str
) -> PendingReviewRecord | None:
    """The `PendingReview` with `review_id`, or `None` when it does not exist (issue #195).

    A plain read like `list_pending_reviews`, but without its resolvable predicate: a review whose
    referenced nodes are gone is still returned, so the resolution attempt can be audited with the
    entity ids the review names.
    """
    result = single_tenant_graph.query(_GET_REVIEW_QUERY, params={"review_id": review_id})
    rows = cast("list[list[object]]", result.result_set)
    return _record_from_row(rows[0]) if rows else None


def _record_from_row(row: list[object]) -> PendingReviewRecord:
    """Map one result row (column order of the list/get queries) to a `PendingReviewRecord`."""
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
    """Resolve one `PendingReview` by id (issue #35: `keep-separate` (Slice 3), `merge` (Slice 4)).

    Step 1 (both decisions): `MATCH (r:PendingReview {id: $review_id})
    RETURN r.kind` -- a plain read, like `list_pending_reviews`. Zero rows
    means `review_id` doesn't exist or was already resolved (§2.1: a
    resolved review's node is deleted outright, never soft-status-changed,
    so the two conditions are indistinguishable and collapse to one) --
    returns `None` immediately, before any write query is issued, so
    AC-BI-008's "no graph changes" half holds by construction on this path
    (nothing has been written yet). See the module docstring for why `None`,
    not a raised exception, is this module's own not-found signal.

    Step 2 (`decision="keep-separate"`, AC-BI-004): one gateway group on `target` whose only
    primitive deletes the `PendingReview` node -- nothing else touched (no other graph node or
    edge changes, issue #210). The review is read first and deleted after, with no lock between:
    a review that vanished in between makes the delete a no-op (the gateway drops it), as the old
    `MATCH ... DELETE` matched nothing. `target` names the `policy_system` graph and the audit
    event of the resolution's opening row; the merge decision does not use it yet (slice 10b).

    Step 2 (`decision="merge"`, AC-BI-005/006/007): delegates to
    `_resolve_merge` -- CHANGES.md H2's combined existence-check read, then
    (only if both referenced nodes still exist) CHANGES.md Appendix C1's
    atomic merge write, all before any log entry is emitted. Raises
    `StalePendingReviewError` if either referenced node is gone (see module
    docstring); returns `None` if the review itself vanished between Step 1
    and the existence-check (a benign, narrow race -- treated the same as a
    genuine not-found, not as "stale").

    On success (either decision), emits one structured log entry
    (AC-BI-009): `component="company_merge"`, `action="resolve_near_miss_review"`,
    `entity_id=review_id`, `outcome=decision` -- `timestamp` is automatic
    (`LogEntry`'s own `default_factory=time`, `ps_service/logging/models.py`),
    matching `merge.py::_log_dedup_decisions`'s own established call shape.
    `decision="merge"` additionally carries `winner_id`/`loser_id` in `extra`
    so the log entry alone identifies which node won (AC-BI-009, full).
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
    """The `decision="merge"` half of `resolve_review`'s Step 2 (AC-BI-005/006/007).

    Issue #210: read, resolve, then write ONE gateway group.

    Read: CHANGES.md H2's combined existence-check, widened to return both nodes' `created_at`.
    Zero rows (the review vanished between `resolve_review`'s own Step 1 and this call -- a benign,
    narrow race) returns `None`, propagated by the caller as a plain not-found. A row with either
    `incoming_exists`/`existing_exists` false raises `StalePendingReviewError` -- before anything
    is submitted, so "no graph changes" holds on this path too.

    Resolve: the winner is decided here, in Python (`_incoming_wins`): earlier `created_at` wins, a
    missing one counts as the earliest, a tie goes to the incoming node (§M2). Then the loser's
    five edge families are read (`_LOSER_EDGE_READS`).

    Write: `UpsertEdge`s putting each of the loser's edges on the winner, then `DeleteNode(loser)`
    (which removes whatever edges remain), then `DeleteNode(PendingReview)`, as one group on
    `target`: all or nothing. No lock spans the reads and the write (C-0, as before): a node
    deleted in between makes the gateway refuse the group on a missing endpoint, and a review
    deleted in between makes its delete a no-op.
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
    """`coalesce(a.created_at, '') <= coalesce(b.created_at, '')`: the old statement's CASE.

    ISO-8601 text compares as text; a missing `created_at` is `''`, which sorts first, so it wins
    over any present one; equal (or both missing) goes to the incoming node. A stored value that is
    not text cannot be compared with text (null in Cypher), so the existing node wins.
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
    """One `UpsertEdge` per edge of the loser's five families, moved onto the winner.

    The other end of each edge was just read from the graph, so it exists; the winner exists (the
    existence check). A repeated identity (a family read twice cannot repeat, but the same edge may
    arise from two families on a self-loop) folds to one. The edge keeps no properties.
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
