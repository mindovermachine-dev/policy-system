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
filter is needed. This is a read, so -- mirroring `graph_reader.py`'s own
"every query here is read-only, no dependency-health wrapper" convention --
it calls `single_tenant_graph.query(...)` directly, not through
`_execute_query` (which stays reserved for this module's writes).

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
the atomic merge write (`_MERGE_QUERY_TEMPLATE`, CHANGES.md Appendix C1) is
ever issued. A stale reference (either id no longer resolves -- e.g. a prior,
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

`_MERGE_QUERY_TEMPLATE`'s `WITH DISTINCT winner, loser` after every
`FOREACH` block (CHANGES.md C1) is the fix for the row-multiplication bug
PLAN.md's original query had: each `OPTIONAL MATCH` can match more than one
relationship (e.g. several `Obligation`s `REQUIRES`-ing the same loser
`Capability` -- `docs/artifacts/ps-domain-concepts.md:652-681`'s own worked
example, the domain's primary scenario, not an edge case), so without
`DISTINCT` collapsing the row count back to 1 after each block, the trailing
`DETACH DELETE loser, rev` would run once per surviving row instead of once.
Verified against a real FalkorDB instance by the required
`falkordb_live`-marked test in `test_pending_review_resolve.py` (issue #35
Slice 4, CHANGES.md H1) -- see that test's own docstring for whether
FOREACH/CASE worked as-is or the UNWIND fallback (CHANGES.md Appendix H1)
was needed.

Own copy of `graph_writer._execute_query`'s connectivity-wrapping shape
(PLAN.md §4.3, mirroring `graph_writer.py`'s own module docstring
precedent): a fresh private copy, not a shared import of another module's
private, underscore-prefixed function.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

import redis.exceptions

from ps_service.company_merge.errors import CompanyMergePersistenceError, StalePendingReviewError
from ps_service.company_merge.models import PendingReviewRecord, ResolveOutcome
from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy
from ps_service.logging import emit_log_entry

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle, GraphQueryResult
    from ps_service.company_merge.models import NearMissPair
    from ps_service.logging import LogEmitter

__all__ = ["list_pending_reviews", "persist_pending_reviews", "resolve_review"]

_LIST_PENDING_REVIEWS_QUERY = (
    "MATCH (r:PendingReview) RETURN r.id, r.kind, r.incoming_id, r.incoming_text, "
    "r.nearest_existing_id, r.nearest_existing_text, r.similarity, r.created_at "
    "ORDER BY r.created_at ASC"
)

_FIND_REVIEW_QUERY = "MATCH (r:PendingReview {id: $review_id}) RETURN r.kind"
_DELETE_REVIEW_QUERY = "MATCH (r:PendingReview {id: $review_id}) DELETE r"

# CHANGES.md H2 -- one combined existence-check read, issued only for
# decision="merge", after the shared _FIND_REVIEW_QUERY above already
# confirmed the review itself exists. `{kind}` is templated (labels are
# static/compile-time in Cypher); `r.incoming_id`/`r.nearest_existing_id`
# are runtime property references off the freshly re-matched `r` (cheap,
# id-indexed), not params -- so this stays a single query per call, not two.
_MERGE_EXISTENCE_CHECK_QUERY_TEMPLATE = (
    "MATCH (r:PendingReview {{id: $review_id}}) "
    "OPTIONAL MATCH (a:{kind} {{id: r.incoming_id}}) "
    "OPTIONAL MATCH (b:{kind} {{id: r.nearest_existing_id}}) "
    "RETURN r.incoming_id AS incoming_id, r.nearest_existing_id AS nearest_existing_id, "
    "a IS NOT NULL AS incoming_exists, b IS NOT NULL AS existing_exists"
)

# CHANGES.md Appendix C1 -- the corrected atomic merge/keep-separate write.
# `WITH DISTINCT winner, loser` after every FOREACH block collapses however
# many rows the preceding OPTIONAL MATCH produced back to exactly 1 before
# the next OPTIONAL MATCH can multiply again -- without it, `DETACH DELETE
# loser, rev` would run once per surviving row instead of once whenever the
# loser has more than one matching edge (the domain's primary scenario, not
# an edge case -- ps-domain-concepts.md:652-681). `{kind}` is templated for
# the same static-label reason as above; `$incoming_id`/`$nearest_existing_id`
# are real params here (already read back by the existence-check query,
# not re-derived from `r`, since the loser node is about to be deleted and
# re-matching by id after that would be pointless). M2 (final, user-confirmed):
# `coalesce(created_at, '') <=` -- a missing/NULL created_at always wins
# (sorts first) over a present one, since a node predating this feature is,
# by definition, older than one minted after.
_MERGE_QUERY_TEMPLATE = (
    "MATCH (a:{kind} {{id: $incoming_id}}), (b:{kind} {{id: $nearest_existing_id}}) "
    "WITH a, b, "
    "CASE WHEN coalesce(a.created_at, '') <= coalesce(b.created_at, '') "
    "THEN a ELSE b END AS winner, "
    "CASE WHEN coalesce(a.created_at, '') <= coalesce(b.created_at, '') "
    "THEN b ELSE a END AS loser "
    "OPTIONAL MATCH (o:Obligation)-[:REQUIRES]->(loser) "
    "FOREACH (_x IN CASE WHEN o IS NOT NULL THEN [1] ELSE [] END | "
    "MERGE (o)-[:REQUIRES]->(winner)) "
    "WITH DISTINCT winner, loser "
    "OPTIONAL MATCH (loser)-[:GOVERNED_BY]->(p:Policy) "
    "FOREACH (_x IN CASE WHEN p IS NOT NULL THEN [1] ELSE [] END | "
    "MERGE (winner)-[:GOVERNED_BY]->(p)) "
    "WITH DISTINCT winner, loser "
    "OPTIONAL MATCH (c:Capability)-[:GOVERNED_BY]->(loser) "
    "FOREACH (_x IN CASE WHEN c IS NOT NULL THEN [1] ELSE [] END | "
    "MERGE (c)-[:GOVERNED_BY]->(winner)) "
    "WITH DISTINCT winner, loser "
    "OPTIONAL MATCH (loser)-[:SUPPORTED_BY]->(s:Standard) "
    "FOREACH (_x IN CASE WHEN s IS NOT NULL THEN [1] ELSE [] END | "
    "MERGE (winner)-[:SUPPORTED_BY]->(s)) "
    "WITH DISTINCT winner, loser "
    "MATCH (rev:PendingReview {{id: $review_id}}) "
    "DETACH DELETE loser, rev "
    "RETURN winner.id AS winner_id, loser.id AS loser_id"
)

_REVIEW_ID_PREFIX = "review_"

_COMPONENT = "company_merge"
_RESOLVE_ACTION = "resolve_near_miss_review"


def _execute_query(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> GraphQueryResult:
    """Wrap every `graph.query()` write in this module for connectivity-health recording.

    Own copy of `graph_writer._execute_query` -- see module docstring.
    """
    try:
        result = graph.query(query, params=params)
    except redis.exceptions.RedisError as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise CompanyMergePersistenceError(f"FalkorDB write failed: {exc}") from exc
    mark_healthy(FALKORDB)
    return result


def persist_pending_reviews(
    single_tenant_graph: GraphHandle,
    near_misses: tuple[NearMissPair, ...],
    *,
    kind: Literal["Capability", "Policy"],
) -> None:
    """Persist one `PendingReview` node per `NearMissPair` (AC-BI-001, AC-BI-002).

    `CREATE`, not `MERGE`: every `NearMissPair` this function receives is, by
    construction, a genuinely new occurrence surfaced during THIS merge run
    -- there is no dedup-of-near-misses concept in this schema
    (`dedup.py` never re-surfaces a pair it already recorded in a prior run).
    A repeat `merge_baseline_graph` run over unchanged input could in
    principle re-surface the same `incoming_id`/`nearest_existing_id` pair
    and create a duplicate `PendingReview` -- an accepted, pre-existing-
    pattern-consistent gap (PLAN.md §4.1): deduplicating review records
    themselves is out of this issue's AC scope (no bulk/batch resolve).

    `id` is minted via `uuid.uuid4().hex`, prefixed `review_` for operator
    readability -- a `PendingReview` is an operational/review record, not a
    regulatory/canonical entity, so it has no stable content to hash
    (PLAN.md §2.4).
    """
    for pair in near_misses:
        _execute_query(
            single_tenant_graph,
            "CREATE (r:PendingReview {id: $id, kind: $kind, status: $status, "
            "incoming_id: $incoming_id, incoming_text: $incoming_text, "
            "nearest_existing_id: $nearest_existing_id, "
            "nearest_existing_text: $nearest_existing_text, "
            "similarity: $similarity, created_at: $created_at})",
            params={
                "id": f"{_REVIEW_ID_PREFIX}{uuid.uuid4().hex}",
                "kind": kind,
                "status": "pending",
                "incoming_id": pair.incoming_id,
                "incoming_text": pair.incoming_text,
                "nearest_existing_id": pair.nearest_existing_id,
                "nearest_existing_text": pair.nearest_existing_text,
                "similarity": pair.similarity,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )


def list_pending_reviews(single_tenant_graph: GraphHandle) -> tuple[PendingReviewRecord, ...]:
    """Every unresolved `PendingReview`, in `created_at` order (AC-BI-003).

    Every `PendingReview` node IS unresolved by construction (§2.1: a
    resolved one is deleted outright, never soft-status-changed) -- no
    `WHERE status = 'pending'` filter is needed, though the `status`
    property is still written at persist time (forward-compatibility /
    observability for an operator inspecting the graph directly).
    """
    result = single_tenant_graph.query(_LIST_PENDING_REVIEWS_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    reviews: list[PendingReviewRecord] = []
    for row in rows:
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
        reviews.append(
            PendingReviewRecord(
                id=cast("str", review_id),
                kind=cast('Literal["Capability", "Policy"]', kind),
                incoming_id=cast("str", incoming_id),
                incoming_text=cast("str", incoming_text),
                nearest_existing_id=cast("str", nearest_existing_id),
                nearest_existing_text=cast("str", nearest_existing_text),
                similarity=cast("float", similarity),
                created_at=cast("str", created_at),
            )
        )
    return tuple(reviews)


def resolve_review(
    single_tenant_graph: GraphHandle,
    review_id: str,
    decision: Literal["merge", "keep-separate"],
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

    Step 2 (`decision="keep-separate"`, AC-BI-004): one write query,
    `MATCH (r:PendingReview {id: $review_id}) DELETE r` -- nothing else
    touched (no other graph node or edge changes).

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
        merge_ids = _resolve_merge(single_tenant_graph, review_id, kind)
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

    _execute_query(single_tenant_graph, _DELETE_REVIEW_QUERY, params={"review_id": review_id})
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
) -> tuple[str, str] | None:
    """The `decision="merge"` half of `resolve_review`'s Step 2 (AC-BI-005/006/007).

    First issues CHANGES.md H2's combined existence-check read. Zero rows
    (the review vanished between `resolve_review`'s own Step 1 and this call
    -- a benign, narrow race) returns `None`, propagated by the caller as a
    plain not-found, identically to Step 1's own zero-rows case. A row with
    either `incoming_exists`/`existing_exists` false raises
    `StalePendingReviewError` -- before the merge write query below is ever
    issued, so "no graph changes" holds on this path too.

    Only once both referenced nodes are confirmed present does the single
    atomic `_MERGE_QUERY_TEMPLATE` write run -- one `GRAPH.QUERY` call:
    determines winner/loser (earlier `created_at` wins, §M2), re-points
    every edge type that can reference a Capability/Policy loser onto the
    winner, deletes the loser node and the `PendingReview` record.
    """
    existence_query = _MERGE_EXISTENCE_CHECK_QUERY_TEMPLATE.format(kind=kind)
    existence_result = single_tenant_graph.query(existence_query, params={"review_id": review_id})
    existence_rows = cast("list[list[object]]", existence_result.result_set)
    if not existence_rows:
        return None
    incoming_id, nearest_existing_id, incoming_exists, existing_exists = existence_rows[0]
    if not incoming_exists or not existing_exists:
        raise StalePendingReviewError(review_id)

    merge_query = _MERGE_QUERY_TEMPLATE.format(kind=kind)
    merge_result = _execute_query(
        single_tenant_graph,
        merge_query,
        params={
            "incoming_id": incoming_id,
            "nearest_existing_id": nearest_existing_id,
            "review_id": review_id,
        },
    )
    merge_rows = cast("list[list[object]]", merge_result.result_set)
    winner_id, loser_id = merge_rows[0]
    return cast("str", winner_id), cast("str", loser_id)
