"""REST-boundary glue for the near-miss review workflow (issue #35, PLAN.md §3.1).

Mirrors `restore_orchestration.py`'s/`ingestion_orchestration.py`'s shape: an
injection seam (:class:`NearMissReviewDependencies`) and
:func:`build_default_near_miss_review_dependencies`, which wires the real
`ps_service.company_merge.pending_review` functions via **function-local**
imports so that importing `ps_service.main` never transitively loads
`ps_service.company_merge` at module load (M6 / the Process Harness
decoupling guarantee).

CHANGES.md M1 (repair resolution, supersedes PLAN.md's original
`open_db`/`single_tenant_graph_name` pair): `RestoreDependencies`
(`restore_orchestration.py:87-92`) was confirmed *not* the right template --
it hands the route a raw `FalkorDB` connection plus a graph name, with graph
selection happening somewhere else not shown in that file. The actually
correct, closer precedent is `ingestion_orchestration.py:1012-1020`'s
`_open_single_tenant_graph(config) -> GraphHandle`, which already does the
full `connect_from_config` -> `select_graph` -> named single-tenant graph
round trip. `_open_single_tenant_graph` below is a byte-for-byte copy of
that function (own private copy, not a shared import -- mirrors
`falkordb_client.py`/`graph_writer.py`'s own "own copy, not shared import"
module-docstring convention, and `pending_review.py`'s own `_execute_query`
precedent).

Slice 3 (AC-BI-004, the not-found half of AC-BI-008, the `keep-separate`
half of AC-BI-009) adds `resolve_review` to this bundle and
`run_resolve_near_miss`, the `POST /near-misses/{review_id}/resolve` entry
point. `pending_review.resolve_review` returns `None` (not a raised
exception) when `review_id` doesn't exist or was already resolved -- this
module is where that `None` is translated into the API-boundary
`PendingReviewNotFoundError` (`api/errors.py`), exactly like
`RestoreArtifactRejectedError`'s own "API-boundary translation of a
lower-layer condition" pattern: `ps_service.company_merge` never imports
from `ps_service.api` (M6 layering is one-directional).

Slice 4 (AC-BI-005/006/007, AC-BI-008/009 completed) widens both to add
`decision="merge"`. `pending_review.resolve_review` raises
`company_merge.errors.StalePendingReviewError` (not `None`) when the merge
branch's existence check (CHANGES.md H2) finds `incoming_id` or
`nearest_existing_id` no longer resolves to a real node -- this module
catches that specific, lightweight, dependency-free exception type
(`ps_service.company_merge.errors` is safe to import at module level here,
mirroring `restore_orchestration.py`'s own `ps_service.restore.errors`
precedent -- unlike `pending_review.py` itself, it is never function-local)
and translates it into `PendingReviewNotFoundError` with H2's dedicated
stale-reference message, before any merge write has happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ps_service.api.errors import PendingReviewNotFoundError
from ps_service.api.models import (
    PendingReviewEntry,
    PendingReviewListResponse,
    ResolveReviewResponse,
)
from ps_service.company_merge.errors import StalePendingReviewError

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.models import PendingReviewRecord, ResolveOutcome
    from ps_service.config import ServiceConfig


@dataclass(frozen=True, slots=True)
class NearMissReviewDependencies:
    """Everything the near-miss review routes need that is not per-request."""

    open_single_tenant_graph: Callable[[ServiceConfig], GraphHandle]
    list_pending_reviews: Callable[[GraphHandle], tuple[PendingReviewRecord, ...]]
    resolve_review: Callable[
        [GraphHandle, str, Literal["keep-separate", "merge"]], ResolveOutcome | None
    ]


def _to_pending_review_entry(record: PendingReviewRecord) -> PendingReviewEntry:
    """Map one `PendingReviewRecord` to the `GET /near-misses` wire entry (AC-BI-003).

    `id`/`kind`/`incoming_text`/`nearest_existing_text`/`similarity` only --
    `incoming_id`/`nearest_existing_id` are deliberately omitted from the
    wire response (PLAN.md §3.2: AC-BI-003 only requires the review's OWN
    id, incoming text, existing text, and similarity score).
    """
    return PendingReviewEntry(
        id=record.id,
        kind=record.kind,
        incoming_text=record.incoming_text,
        nearest_existing_text=record.nearest_existing_text,
        similarity=record.similarity,
    )


def run_list_near_misses(
    *, config: ServiceConfig, dependencies: NearMissReviewDependencies
) -> PendingReviewListResponse:
    """Return every unresolved `PendingReview` as a `PendingReviewListResponse` (AC-BI-003).

    Args:
        config: The resolved service configuration (injected).
        dependencies: The near-miss review dependency bundle (injected;
            overridden in tests).

    Returns:
        A :class:`PendingReviewListResponse` carrying one entry per
        unresolved `PendingReview` node, in the order
        `pending_review.list_pending_reviews` returned them.
    """
    graph = dependencies.open_single_tenant_graph(config)
    records = dependencies.list_pending_reviews(graph)
    return PendingReviewListResponse(reviews=[_to_pending_review_entry(r) for r in records])


def _to_resolve_response(outcome: ResolveOutcome) -> ResolveReviewResponse:
    """Map a `ResolveOutcome` to the `POST /near-misses/{review_id}/resolve` success body."""
    return ResolveReviewResponse(
        review_id=outcome.review_id,
        decision=outcome.decision,
        winner_id=outcome.winner_id,
        loser_id=outcome.loser_id,
    )


def run_resolve_near_miss(
    review_id: str,
    decision: Literal["keep-separate", "merge"],
    *,
    config: ServiceConfig,
    dependencies: NearMissReviewDependencies,
) -> ResolveReviewResponse:
    """Resolve one `PendingReview` (issue #35, `POST /near-misses/{review_id}/resolve`).

    AC-BI-004: `decision="keep-separate"` deletes only the `PendingReview`
    record, nothing else. AC-BI-005/006/007: `decision="merge"` re-points
    every edge referencing the loser canonical node onto the
    deterministically-chosen winner, deletes the loser, and deletes the
    `PendingReview` record, all as one atomic write.

    AC-BI-008 (not-found half, both decisions): raises
    `PendingReviewNotFoundError` (-> HTTP 404) when `review_id` doesn't
    exist or was already resolved -- the API-boundary translation of
    `pending_review.resolve_review`'s `None` return (see module docstring);
    no write happens on this path. For `decision="merge"` specifically,
    also raises `PendingReviewNotFoundError` (same status, a dedicated
    "stale" message) when `pending_review.resolve_review` raises
    `StalePendingReviewError` -- the review exists but the incoming/existing
    canonical node it references no longer does (already resolved by a
    prior merge); no merge write happens on this path either (CHANGES.md
    H2).

    Args:
        review_id: The `PendingReview` id to resolve (path parameter).
        decision: How to resolve it -- `"keep-separate"` or `"merge"`.
        config: The resolved service configuration (injected).
        dependencies: The near-miss review dependency bundle (injected;
            overridden in tests).

    Returns:
        A :class:`ResolveReviewResponse` naming the resolved review and
        decision (`winner_id`/`loser_id` populated for `"merge"` only).

    Raises:
        PendingReviewNotFoundError: `review_id` doesn't exist, was already
            resolved, or (merge only) references a node a prior merge
            already deleted.
    """
    graph = dependencies.open_single_tenant_graph(config)
    try:
        outcome = dependencies.resolve_review(graph, review_id, decision)
    except StalePendingReviewError as exc:
        raise PendingReviewNotFoundError(
            f"pending review {review_id!r} references a node that no longer exists "
            "(already resolved by a prior merge); this review is now stale"
        ) from exc
    if outcome is None:
        raise PendingReviewNotFoundError(f"no unresolved PendingReview with id {review_id!r}")
    return _to_resolve_response(outcome)


# --- default wiring (M6 -- every company_merge import below is function-local) ---


def _open_single_tenant_graph(config: ServiceConfig) -> GraphHandle:
    """Open the single-tenant (``policy_system``) graph.

    Byte-for-byte copy of `ingestion_orchestration._open_single_tenant_graph`
    (CHANGES.md M1) -- an independent copy, not a shared import, mirroring
    this component's established "own copy per module" convention.
    """
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Company Merge at import
        connect_from_config,
        select_graph,
        single_tenant_graph_name,
    )

    return select_graph(connect_from_config(config), single_tenant_graph_name())


def build_default_near_miss_review_dependencies() -> NearMissReviewDependencies:
    """Wire the real `pending_review` functions into a `NearMissReviewDependencies`.

    `ps_service.company_merge.pending_review` is imported **function-locally**
    so that importing `ps_service.main` never transitively loads
    `ps_service.company_merge` at module load (M6) -- mirrors
    `build_default_restore_dependencies`/`build_default_pipeline_dependencies`
    exactly.

    Returns:
        A :class:`NearMissReviewDependencies` bound to the production
        `list_pending_reviews` and the real single-tenant-graph opener.
    """
    from ps_service.company_merge.pending_review import (  # noqa: PLC0415 -- M6: function-local
        list_pending_reviews,
        resolve_review,
    )

    return NearMissReviewDependencies(
        open_single_tenant_graph=_open_single_tenant_graph,
        list_pending_reviews=list_pending_reviews,
        resolve_review=resolve_review,
    )
