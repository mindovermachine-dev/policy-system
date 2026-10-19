"""Canonical-node identity resolution for Company Merge: exact-key and semantic.

Decides, for every incoming baseline node of one `ReviewableKind`, which canonical node of the
single-tenant graph it resolves to, and which similar pairs are recorded as near misses. The kind's
spec supplies the node label and text property; this module names neither.

Two entry points run the same algorithm. `dedupe_canonical_nodes` is the live path and calls
`route_embedding` for any embedding it lacks. `resolve_capability_convergence_offline` is the
offline counterpart a restore's baseline merge uses: every embedding is artifact-supplied or
already cached, and it never fetches one through `route_embedding` or an `EmbeddingCaller`.
Both paths grow a working index as nodes are minted, so same-run EXACT convergence works. Same-run
SEMANTIC convergence is deliberately excluded: the semantic-match candidate pool is restricted to
nodes that existed before the run, so a same-run near-duplicate is recorded as a `NearMissPair`
and never merged.

A kind's scope decides the candidate pool (`"global"`: every node of the label; `"per-instrument"`:
the nodes the run's instrument `DEFINES` in the single-tenant graph, plus the run's own mints). Its
rule decides what a close pair does (`"merge-above"`: merge onto an eligible candidate at or above
the merge threshold; `"review-only"`: never merge, cite pairs at or above the review threshold).
Capability is the one kind with a merge rule in production; Obligation is Role-scoped and passed
through.
The exact-key match is only correct if it computes the same hash Domain Mapper used to write the
baseline graph's node ids, so `capability_id` is imported from Domain Mapper, never reimplemented
here.

A Capability that a Compliance Officer cleanup merge absorbed no longer exists in the graph, so it
is never an exact-match or semantic-match candidate. Its incoming id is resolved to the survivor
before dedup runs (`company_merge.redirects.resolve_redirects`), so this module sees only the
nodes that remain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ps_service.company_merge.errors import CompanyMergeConfigurationError
from ps_service.company_merge.falkordb_client import (
    GraphHandle,  # noqa: TC001 — introspected at runtime by test_ac008_out_of_scope via typing.get_type_hints
)
from ps_service.company_merge.models import (
    BaselineNode,
    CanonicalResolution,
    DedupResult,
    ExistingCanonicalNode,
    NearMissPair,
    SemanticMatchResult,
)
from ps_service.company_merge.reviewable_kinds import (
    ReviewableKind,
    ReviewThresholds,
    require_registered,
    require_thresholds,
)
from ps_service.company_merge.similarity import cosine_similarity
from ps_service.domain_mapper.identity import capability_id
from ps_service.llm_interface.client import (
    EmbeddingCaller,  # noqa: TC001 — introspected at runtime by test_ac008_out_of_scope via typing.get_type_hints
)
from ps_service.llm_interface.embedding import route_embedding
from ps_service.logging.emitter import (
    LogEmitter,  # noqa: TC001 — introspected at runtime by test_ac008_out_of_scope via typing.get_type_hints
)
from ps_service.logging.facade import emit_log_entry

__all__ = [
    "capability_id",
    "dedupe_canonical_nodes",
    "find_best_semantic_match",
    "read_existing_canonical_index",
    "resolve_capability_convergence_offline",
    "resolve_exact_match",
]


def read_existing_canonical_index(
    single_tenant_graph: GraphHandle, kind: ReviewableKind, instrument_id: str | None = None
) -> tuple[ExistingCanonicalNode, ...]:
    """Read the existing nodes of `kind` that are candidates for a run, as a canonical index.

    A `"global"` kind reads every node of its label. A `"per-instrument"` kind reads only the nodes
    the run's instrument `DEFINES` , so a node another instrument defines is never
    a candidate; the instrument id is a bound parameter.

    The label and text property come from `kind`, a code-owned spec whose fields are validated as
    plain identifiers when it is built (`ReviewableKind.__post_init__`), never adapter-, LLM- or
    user-sourced, so they are interpolated into the query directly. The property read onto
    `ExistingCanonicalNode.text` is `kind.text_property`. Read-only.

    Args:
        single_tenant_graph: Read handle on the single-tenant graph.
        kind: The kind whose nodes are read.
        instrument_id: The run's instrument; required when `kind.scope` is `"per-instrument"`.

    Returns:
        One `ExistingCanonicalNode` per node, in query order; empty when the graph has none.
        `embedding` is the cached vector, or `None` when it has never been computed.
    """
    if kind.scope == "per-instrument":
        result = single_tenant_graph.query(
            "MATCH (:RegulatoryInstrument {id: $instrument_id})-[:DEFINES]->"
            f"(n:{kind.label}) RETURN n.id, n.{kind.text_property}, n.embedding",
            {"instrument_id": instrument_id},
        )
    else:
        result = single_tenant_graph.query(
            f"MATCH (n:{kind.label}) RETURN n.id, n.{kind.text_property}, n.embedding"
        )
    rows = cast("list[list[object]]", result.result_set)
    nodes: list[ExistingCanonicalNode] = []
    for row in rows:
        node_id, text, embedding = row
        raw_embedding = cast("list[float] | None", embedding)
        nodes.append(
            ExistingCanonicalNode(
                id=cast("str", node_id),
                text=cast("str", text),
                embedding=tuple(raw_embedding) if raw_embedding is not None else None,
            )
        )
    return tuple(nodes)


def resolve_exact_match(incoming_id: str, existing_ids: frozenset[str]) -> bool:
    """Report whether `incoming_id` is already a canonical node id.

    Domain Mapper computed every baseline node's id with the canonical hash, so the incoming
    node's own `id` already equals its canonical id; this is a plain membership check.

    Args:
        incoming_id: Id of the incoming baseline node.
        existing_ids: Ids of the canonical nodes already present.

    Returns:
        True when `incoming_id` is in `existing_ids`.
    """
    return incoming_id in existing_ids


def find_best_semantic_match(
    incoming_text: str,
    existing_index: tuple[ExistingCanonicalNode, ...],
    *,
    model: str,
    call_embedding: EmbeddingCaller | None = None,
    emitter: LogEmitter | None = None,
    eligible_ids: frozenset[str] | None = None,
    incoming_embedding: tuple[float, ...] | None = None,
) -> SemanticMatchResult | None:
    """Score an incoming text against every canonical entry and return the best matches.

    Each entry's embedding is its cached `embedding` when present, otherwise computed with one
    `route_embedding` call for `entry.text`. The incoming embedding is `incoming_embedding` when
    supplied (no call is made for it), otherwise computed once with `route_embedding`.
    `similarity.cosine_similarity` scores every entry against it. No merge or review cut-off is
    applied here; the caller decides merge versus surface.

    Two maxima come from the single scan at no extra embedding cost: the overall best
    (`best_existing_id`, `best_similarity`, used only to cite a near miss) and the best restricted
    to `eligible_ids` (`best_eligible_id`, `best_eligible_similarity`, used only for the merge
    decision). Every entry that needed a fresh embedding is returned in
    `newly_computed_existing_embeddings`, so a caller can reuse it in memory and persist it with
    `graph_writer.backfill_canonical_embeddings`.

    Args:
        incoming_text: Text of the incoming node, embedded unless `incoming_embedding` is given.
        existing_index: Canonical entries to compare against.
        model: Embedding model name passed to `route_embedding`.
        call_embedding: Embedding caller override; None uses the default provider routing.
        emitter: Log sink passed to `route_embedding`; None uses the default emitter.
        eligible_ids: Ids allowed as the merge target. None treats every entry as eligible, so
            the eligible best equals the overall best; an empty set (or one matching no scanned
            entry) leaves both eligible fields None.
        incoming_embedding: Cached embedding of the incoming node, reused as is.

    Returns:
        A `SemanticMatchResult`, or None when `existing_index` is empty. None means the incoming
        node is a first-time mint, and no `route_embedding` call was made, not even for
        `incoming_text`.

    Raises:
        LlmProviderError: Propagated unchanged from any `route_embedding` call.
    """
    if not existing_index:
        return None

    if incoming_embedding is None:
        incoming_result = route_embedding(
            incoming_text, model=model, call_embedding=call_embedding, emitter=emitter
        )
        incoming_embedding = tuple(incoming_result.vector)

    newly_computed_existing_embeddings: dict[str, tuple[float, ...]] = {}
    scored: list[tuple[str, float]] = []
    for entry in existing_index:
        if entry.embedding is not None:
            candidate_embedding = entry.embedding
        else:
            computed = route_embedding(
                entry.text, model=model, call_embedding=call_embedding, emitter=emitter
            )
            candidate_embedding = tuple(computed.vector)
            newly_computed_existing_embeddings[entry.id] = candidate_embedding
        scored.append((entry.id, cosine_similarity(incoming_embedding, candidate_embedding)))

    best_existing_id, best_similarity = max(scored, key=lambda pair: pair[1])

    eligible_scored = (
        scored if eligible_ids is None else [pair for pair in scored if pair[0] in eligible_ids]
    )
    best_eligible_id, best_eligible_similarity = (
        max(eligible_scored, key=lambda pair: pair[1]) if eligible_scored else (None, None)
    )

    return SemanticMatchResult(
        best_existing_id=best_existing_id,
        best_similarity=best_similarity,
        best_eligible_id=best_eligible_id,
        best_eligible_similarity=best_eligible_similarity,
        incoming_embedding=incoming_embedding,
        newly_computed_existing_embeddings=newly_computed_existing_embeddings,
    )


def _merge_target_id(result: SemanticMatchResult | None, threshold: float | None) -> str | None:
    """Return the eligible canonical id to merge onto, or `None` to mint instead.

    `None` when there is no semantic-match result, no scanned entry was eligible (both
    `best_eligible_*` fields are `None`), or the eligible candidate's score is below `threshold`.
    The caller mints a new canonical node in each of those cases.
    """
    if (
        result is None
        or threshold is None
        or result.best_eligible_id is None
        or result.best_eligible_similarity is None
        or result.best_eligible_similarity < threshold
    ):
        return None
    return result.best_eligible_id


def _record_excluded_same_run_mint_near_miss(
    node: BaselineNode,
    node_text: str,
    result: SemanticMatchResult,
    working_index: dict[str, ExistingCanonicalNode],
    threshold: float,
    near_misses: list[NearMissPair],
) -> None:
    """Record the near miss hidden behind a merge onto a lower-scoring eligible candidate.

    Called only from the merge branch, where `result.best_eligible_id` cleared `threshold`. If the
    overall best candidate (`result.best_existing_id`) differs from the eligible one and also
    clears `threshold`, it must be a same-run mint: an eligible entry holding the global maximum
    is also the maximum of the eligible subset, so a different overall best is not in
    `eligible_ids`. Appends a `NearMissPair` citing it to `near_misses`; otherwise does nothing.
    The mint branch builds its own near miss, and mint and merge are exclusive per node, so the
    two never double up.
    """
    if result.best_existing_id != result.best_eligible_id and result.best_similarity >= threshold:
        near_misses.append(
            NearMissPair(
                incoming_id=node.id,
                incoming_text=node_text,
                nearest_existing_id=result.best_existing_id,
                nearest_existing_text=working_index[result.best_existing_id].text,
                similarity=result.best_similarity,
            )
        )


def _fold_semantic_match_result(
    node: BaselineNode,
    result: SemanticMatchResult,
    *,
    working_index: dict[str, ExistingCanonicalNode],
    original_existing_ids: frozenset[str],
    embedding_backfills: dict[str, tuple[float, ...]],
    incoming_embedding_backfills: dict[str, tuple[float, ...]],
) -> None:
    """Fold one `find_best_semantic_match` result into the running dedup state.

    Every freshly computed existing embedding is folded into `working_index` at once, so a later
    incoming node comparing against the same entry makes no further embedding call. If the entry
    was in the original pre-run index (genuinely pre-existing, not minted this run) it is also
    recorded in `embedding_backfills`, for `graph_writer.backfill_canonical_embeddings` to persist.

    The incoming node's own embedding is recorded in `incoming_embedding_backfills` only when it
    came in uncached (`node.embedding is None`); a cached one is reused, so there is nothing new
    to persist.
    """
    for existing_id, embedding in result.newly_computed_existing_embeddings.items():
        working_index[existing_id] = ExistingCanonicalNode(
            id=existing_id,
            text=working_index[existing_id].text,
            embedding=embedding,
        )
        if existing_id in original_existing_ids:
            embedding_backfills[existing_id] = embedding
    if node.embedding is None:
        incoming_embedding_backfills[node.id] = result.incoming_embedding


def _incoming_text(node: BaselineNode, kind: ReviewableKind) -> str:
    """Return an incoming node's own text: the property `kind.text_property` names.

    Uses the same property keys as `graph_reader.read_baseline_graph`. It is a separate lookup from
    `read_existing_canonical_index`, which reads a query column rather than a
    `BaselineNode.properties` entry, although both draw on `kind.text_property`.
    """
    return cast("str", node.properties[kind.text_property])


def _validate_run(
    kind: ReviewableKind, thresholds: ReviewThresholds, instrument_id: str | None
) -> None:
    """Fail closed on a kind, thresholds or scope a dedup run cannot use.

    Runs before any read or embedding, so a refusal leaves the graph and the provider untouched.

    Raises:
        UnknownReviewKindError: `kind` is not the spec registered for its label.
        CompanyMergeConfigurationError: A threshold the rule needs is missing or one it cannot use
            was given; or a `"per-instrument"` kind has no `instrument_id`.
    """
    require_registered(kind)
    require_thresholds(kind, thresholds)
    if kind.scope == "per-instrument" and instrument_id is None:
        raise CompanyMergeConfigurationError(
            f"{kind.label}: scope {kind.scope!r} requires an instrument id"
        )


def _eligible_merge_targets(
    kind: ReviewableKind, original_existing_ids: frozenset[str]
) -> frozenset[str]:
    """Return the ids a node may be merged onto: the pre-run nodes, or none for `"review-only"`.

    A `"review-only"` kind never merges, so no candidate is eligible; every close pair is then
    only cited as a near miss.
    """
    return frozenset() if kind.rule == "review-only" else original_existing_ids


def _clears_review_floor(similarity: float, thresholds: ReviewThresholds) -> bool:
    """Report whether a near miss scoring `similarity` is surfaced: no floor, or at or above it.

    The floor is inclusive: a pair scoring exactly `thresholds.review` is surfaced.
    """
    return thresholds.review is None or similarity >= thresholds.review


def dedupe_canonical_nodes(
    incoming_nodes: tuple[BaselineNode, ...],
    *,
    kind: ReviewableKind,
    single_tenant_graph: GraphHandle,
    model: str,
    thresholds: ReviewThresholds,
    instrument_id: str | None = None,
    call_embedding: EmbeddingCaller | None = None,
    emitter: LogEmitter | None = None,
) -> DedupResult:
    """Resolve every incoming node to a canonical id, before anything is written.

    Runs over the whole incoming collection first so `merge.py` writes nothing until every
    resolution is known. It makes one index read and
    never writes, so an `LlmProviderError` aborts with no partial write.

    For each incoming node, in order: an exact-key
    match against the working index (which grows as nodes are minted in this run), then a semantic
    match. Only nodes in the original pre-run index are eligible merge targets, so a same-run
    near-duplicate is never merged, only recorded as a `NearMissPair`. Existing embeddings computed
    during a scan are folded into the working index at once and, for pre-existing entries, recorded
    in `embedding_backfills`. No result, no eligible candidate, or an eligible best score below
    `thresholds.merge` mints a new canonical node. It records a near miss only when the overall
    best neighbour's score is at or above `thresholds.review`; a lower score records none, and
    `thresholds.review` of `None` applies no floor (the production Capability path always sets
    one).
    An eligible best at or above `thresholds.merge` resolves onto that canonical id and may also
    record a second near miss when an ineligible same-run mint scored higher.

    An incoming node whose `embedding` is cached is passed to `find_best_semantic_match` and costs
    no `route_embedding` call. A freshly computed incoming embedding is recorded in
    `DedupResult.incoming_embedding_backfills`, whether the node then mints or merges, so
    `merge.py` can persist it on the baseline graph. Nothing is recorded when no scan ran (empty
    index) or the embedding was already cached.

    Args:
        incoming_nodes: Baseline nodes of one kind, in processing order.
        kind: The registered spec of the nodes' kind; it names the label and text property.
        single_tenant_graph: Read handle on the single-tenant graph holding the canonical nodes.
        model: Embedding model name for any `route_embedding` call.
        thresholds: `merge` is the minimum cosine similarity for an eligible candidate to be merged
            onto; `review` is the floor for surfacing a near miss: a pair scoring at or above it
            is surfaced, a pair below it is not (`None`: no floor, so each best neighbour that was
            scored is surfaced).
        instrument_id: The run's instrument; required by a `"per-instrument"` kind.
        call_embedding: Embedding caller override; None uses the default provider routing.
        emitter: Log sink passed to `route_embedding`; None uses the default emitter.

    Returns:
        A `DedupResult` with one resolution per incoming node in input order, the near misses, and
        the embedding backfills for existing and incoming nodes.

    Raises:
        UnknownReviewKindError: `kind` is not registered; nothing was read or embedded.
        CompanyMergeConfigurationError: `thresholds` or `instrument_id` do not fit `kind`; nothing
            was read or embedded.
        LlmProviderError: Propagated unchanged from an embedding call.
    """
    _validate_run(kind, thresholds, instrument_id)
    merge_threshold = thresholds.merge
    existing_index = read_existing_canonical_index(single_tenant_graph, kind, instrument_id)
    # Never mutated: distinguishes a genuinely pre-existing canonical node (a backfill
    # candidate, an eligible merge target) from one minted later in this run.
    original_existing_ids = frozenset(n.id for n in existing_index)
    eligible_ids = _eligible_merge_targets(kind, original_existing_ids)
    working_index: dict[str, ExistingCanonicalNode] = {n.id: n for n in existing_index}
    embedding_backfills: dict[str, tuple[float, ...]] = {}
    incoming_embedding_backfills: dict[str, tuple[float, ...]] = {}
    resolutions: list[CanonicalResolution] = []
    near_misses: list[NearMissPair] = []

    for node in incoming_nodes:
        node_text = _incoming_text(node, kind)
        existing_ids = frozenset(working_index)

        if resolve_exact_match(node.id, existing_ids):
            resolutions.append(
                CanonicalResolution(
                    incoming_id=node.id,
                    canonical_id=node.id,
                    match_kind="exact",
                    embedding=None,
                )
            )
            continue

        result = find_best_semantic_match(
            node_text,
            tuple(working_index.values()),
            model=model,
            call_embedding=call_embedding,
            emitter=emitter,
            eligible_ids=eligible_ids,
            incoming_embedding=node.embedding,
        )

        if result is not None:
            _fold_semantic_match_result(
                node,
                result,
                working_index=working_index,
                original_existing_ids=original_existing_ids,
                embedding_backfills=embedding_backfills,
                incoming_embedding_backfills=incoming_embedding_backfills,
            )

        merge_target_id = _merge_target_id(result, merge_threshold)

        if merge_target_id is None:
            own_embedding = result.incoming_embedding if result is not None else node.embedding
            resolutions.append(
                CanonicalResolution(
                    incoming_id=node.id,
                    canonical_id=node.id,
                    match_kind="new",
                    embedding=own_embedding,
                )
            )
            # Same-run EXACT convergence works through resolve_exact_match's unrestricted pool.
            # Same-run SEMANTIC convergence is excluded: the candidate pool passed to
            # find_best_semantic_match is restricted to original pre-run nodes
            # (eligible_ids=original_existing_ids), so a same-run mint is never an eligible merge
            # target. The new node still enters the working index so a later incoming node's
            # EXACT match can find it.
            working_index[node.id] = ExistingCanonicalNode(
                id=node.id, text=node_text, embedding=own_embedding
            )
            if result is not None and _clears_review_floor(result.best_similarity, thresholds):
                near_misses.append(
                    NearMissPair(
                        incoming_id=node.id,
                        incoming_text=node_text,
                        nearest_existing_id=result.best_existing_id,
                        nearest_existing_text=working_index[result.best_existing_id].text,
                        similarity=result.best_similarity,
                    )
                )
        else:
            resolutions.append(
                CanonicalResolution(
                    incoming_id=node.id,
                    canonical_id=merge_target_id,
                    match_kind="semantic",
                    embedding=None,
                )
            )
            # `_merge_target_id` returns non-None only when `result` is not None, so `result`
            # is narrowed with a cast rather than a redundant check.
            _record_excluded_same_run_mint_near_miss(
                node,
                node_text,
                cast("SemanticMatchResult", result),
                working_index,
                cast("float", merge_threshold),
                near_misses,
            )

    return DedupResult(
        resolutions=tuple(resolutions),
        near_misses=tuple(near_misses),
        embedding_backfills=embedding_backfills,
        incoming_embedding_backfills=incoming_embedding_backfills,
    )


@dataclass(frozen=True, slots=True)
class _OfflineMatchResult:
    """Result of `_best_offline_match`, the offline twin of `SemanticMatchResult`.

    Attributes:
        best_eligible: Best-scoring scorable candidate restricted to `eligible_ids`, used only for
            the merge decision; None when no scorable candidate is eligible.
        best_eligible_score: Its score, or 0.0 when `best_eligible` is None.
        best_overall: Best-scoring scorable candidate across every entry scanned, used only to
            cite a near miss; None when nothing was scorable.
        best_overall_score: Its score, or 0.0 when `best_overall` is None.

    Both pairs come from the one scan of scorable candidates, at no extra cost.
    """

    best_eligible: ExistingCanonicalNode | None
    best_eligible_score: float
    best_overall: ExistingCanonicalNode | None
    best_overall_score: float


def _best_offline_match(
    own_embedding: tuple[float, ...] | None,
    working_index: dict[str, ExistingCanonicalNode],
    eligible_ids: frozenset[str],
) -> _OfflineMatchResult:
    """Score an artifact-supplied embedding against every scorable candidate.

    A candidate is scorable only if it already carries a cached embedding; one without is skipped,
    never fetched. Returns both the overall best scorable candidate and the best restricted to
    `eligible_ids` (see `_OfflineMatchResult`). Every field is `None`/`0.0` when `own_embedding`
    is `None` or no candidate is scorable; the eligible pair alone is `None`/`0.0` when no
    scorable candidate is eligible. The caller mints whenever `best_eligible` is `None`.
    """
    scorable: list[tuple[ExistingCanonicalNode, tuple[float, ...]]] = [
        (candidate, candidate.embedding)
        for candidate in working_index.values()
        if candidate.embedding is not None
    ]
    if own_embedding is None or not scorable:
        return _OfflineMatchResult(None, 0.0, None, 0.0)

    scored = [
        (candidate, cosine_similarity(own_embedding, embedding))
        for candidate, embedding in scorable
    ]
    best_overall, best_overall_score = max(scored, key=lambda pair: pair[1])

    eligible_scored = [pair for pair in scored if pair[0].id in eligible_ids]
    best_eligible, best_eligible_score = (
        max(eligible_scored, key=lambda pair: pair[1]) if eligible_scored else (None, 0.0)
    )

    return _OfflineMatchResult(
        best_eligible=best_eligible,
        best_eligible_score=best_eligible_score,
        best_overall=best_overall,
        best_overall_score=best_overall_score,
    )


def _record_excluded_same_run_mint_near_miss_offline(
    node: BaselineNode,
    node_text: str,
    match: _OfflineMatchResult,
    best_eligible: ExistingCanonicalNode,
    threshold: float,
    near_misses: list[NearMissPair],
) -> None:
    """Offline twin of `_record_excluded_same_run_mint_near_miss`.

    Called only from the merge branch, where `best_eligible` already cleared `threshold`. If the
    overall best scorable candidate (`match.best_overall`) differs from `best_eligible` and also
    clears `threshold`, it must be a same-run mint, by the same argument as the live twin. Appends
    a `NearMissPair` citing it to `near_misses`; otherwise does nothing.
    """
    if (
        match.best_overall is not None
        and match.best_overall.id != best_eligible.id
        and match.best_overall_score >= threshold
    ):
        near_misses.append(
            NearMissPair(
                incoming_id=node.id,
                incoming_text=node_text,
                nearest_existing_id=match.best_overall.id,
                nearest_existing_text=match.best_overall.text,
                similarity=match.best_overall_score,
            )
        )


def resolve_capability_convergence_offline(
    incoming_nodes: tuple[BaselineNode, ...],
    *,
    incoming_embeddings: dict[str, tuple[float, ...]],
    single_tenant_graph: GraphHandle,
    kind: ReviewableKind,
    thresholds: ReviewThresholds,
    instrument_id: str | None = None,
    emitter: LogEmitter | None = None,
) -> DedupResult:
    """Resolve incoming nodes to canonical ids without any embedding fetch, for a restore.

    Mirrors `dedupe_canonical_nodes` over the whole `incoming_nodes` batch in one call: exact
    match (`resolve_exact_match`), then a semantic match scored with
    `cosine_similarity` alone against `incoming_embeddings` and the existing index's cached
    embeddings. A restore has no live LLM provider, so it never calls `route_embedding` or an
    `EmbeddingCaller`. An existing candidate with no cached embedding is skipped, not fetched. A
    newly minted node is folded into the working index at once, so a later node in the batch
    converges onto it by exact match; same-run SEMANTIC convergence is excluded, as on the live
    path.

    Args:
        incoming_nodes: Baseline nodes of one kind, in processing order.
        incoming_embeddings: Artifact-supplied embedding per incoming node id. A node with no entry
            has no embedding and is minted without a semantic comparison.
        single_tenant_graph: Read handle on the single-tenant graph holding the canonical nodes.
        kind: The registered spec of the nodes' kind; selects the text property and read query
            exactly as `dedupe_canonical_nodes` does.
        thresholds: As for `dedupe_canonical_nodes`.
        instrument_id: The run's instrument; required by a `"per-instrument"` kind.
        emitter: Log sink for the warning entry; None uses the default emitter.

    Returns:
        A `DedupResult` with one resolution per incoming node and the near misses.
        `embedding_backfills` and `incoming_embedding_backfills` are empty because nothing is
        computed.

    Raises:
        UnknownReviewKindError: `kind` is not registered; nothing was read.
        CompanyMergeConfigurationError: `thresholds` or `instrument_id` do not fit `kind`; nothing
            was read.

    Side Effects:
        When any existing candidate was skipped for lacking a cached embedding, emits one
        aggregate log entry with `outcome="warning"` carrying the skipped count.
    """
    _validate_run(kind, thresholds, instrument_id)
    merge_threshold = thresholds.merge
    existing_index = read_existing_canonical_index(single_tenant_graph, kind, instrument_id)
    # Never mutated: distinguishes a genuinely pre-existing canonical node from one minted later
    # in this restore run.
    original_existing_ids = frozenset(n.id for n in existing_index)
    eligible_ids = _eligible_merge_targets(kind, original_existing_ids)
    working_index: dict[str, ExistingCanonicalNode] = {node.id: node for node in existing_index}
    resolutions: list[CanonicalResolution] = []
    near_misses: list[NearMissPair] = []
    skipped_count = 0

    for node in incoming_nodes:
        node_text = _incoming_text(node, kind)
        if resolve_exact_match(node.id, frozenset(working_index)):
            resolutions.append(
                CanonicalResolution(
                    incoming_id=node.id, canonical_id=node.id, match_kind="exact", embedding=None
                )
            )
            continue

        own_embedding = incoming_embeddings.get(node.id)
        skipped_count += sum(
            1 for candidate in working_index.values() if candidate.embedding is None
        )
        match = _best_offline_match(own_embedding, working_index, eligible_ids)
        best_eligible = match.best_eligible

        if (
            best_eligible is None
            or merge_threshold is None
            or match.best_eligible_score < merge_threshold
        ):
            resolutions.append(
                CanonicalResolution(
                    incoming_id=node.id,
                    canonical_id=node.id,
                    match_kind="new",
                    embedding=own_embedding,
                )
            )
            if match.best_overall is not None and _clears_review_floor(
                match.best_overall_score, thresholds
            ):
                near_misses.append(
                    NearMissPair(
                        incoming_id=node.id,
                        incoming_text=node_text,
                        nearest_existing_id=match.best_overall.id,
                        nearest_existing_text=match.best_overall.text,
                        similarity=match.best_overall_score,
                    )
                )
            # Same-run EXACT convergence works through resolve_exact_match's unrestricted pool.
            # Same-run SEMANTIC convergence is excluded: the eligible pool passed to
            # _best_offline_match is restricted to original pre-run nodes, so a same-run mint is
            # never an eligible merge target. The new node still enters the working index so a
            # later node in this artifact can find it by EXACT match.
            working_index[node.id] = ExistingCanonicalNode(
                id=node.id, text=node_text, embedding=own_embedding
            )
        else:
            resolutions.append(
                CanonicalResolution(
                    incoming_id=node.id,
                    canonical_id=best_eligible.id,
                    match_kind="semantic",
                    embedding=None,
                )
            )
            _record_excluded_same_run_mint_near_miss_offline(
                node, node_text, match, best_eligible, merge_threshold, near_misses
            )

    if skipped_count:
        emit_log_entry(
            component="company_merge",
            action="resolve_capability_convergence_offline",
            outcome="warning",
            extra={"skipped_missing_embedding_count": skipped_count},
            emitter=emitter,
        )

    return DedupResult(
        resolutions=tuple(resolutions), near_misses=tuple(near_misses), embedding_backfills={}
    )
