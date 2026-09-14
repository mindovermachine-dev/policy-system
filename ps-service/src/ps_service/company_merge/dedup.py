"""ps_service.company_merge.dedup -- exact-key identity reuse (PLAN_REVIEWED.md §3).

Also the existing-canonical-index reader, exact-match resolution,
semantic-match resolution, and the combined whole-collection resolution
algorithm (PLAN_REVIEWED.md §5.1/§5.2/§5.3/§5.4, Increments 6-9) --
`dedupe_canonical_nodes`, the LIVE path (calls `route_embedding` for any
embedding it lacks). `resolve_capability_convergence_offline` (PLAN.md D6,
Slices 5.3/5.4) is the OFFLINE counterpart a restore's baseline merge uses
instead: structurally the same working-index-growth mechanism, but every
embedding is either artifact-supplied or already cached -- never fetched
via `route_embedding`/`EmbeddingCaller` (enforced by an AST scan,
`tests/company_merge/test_dedup_offline_no_route_embedding_import.py`). On
both paths, that growth mechanism admits same-run EXACT convergence only
(via `resolve_exact_match`'s unrestricted pool) -- same-run SEMANTIC
convergence is deliberately excluded (issue #30): the semantic-match
candidate pool is always restricted to nodes that already existed before
the run started, so a same-run near-duplicate is recorded as a
`NearMissPair`, never merged.

Since issue #42, Company Merge dedupes **Capability only** on the regulatory
spine (Obligation is Role-scoped and passed through). Company Merge's
exact-key match is only correct if it computes *the same hash* Domain Mapper
already used to write the baseline graph's node ids -- so `capability_id` is
imported directly here, never reimplemented. See
`tests/company_merge/test_identity_reuse.py` for the enforcement proof: an
AST scan confirms no function named `capability_id`/`_hash`/`_slug` is ever
defined anywhere in this package, plus a direct byte-for-byte comparison
against `ps_service.domain_mapper.identity`'s own function.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

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
from ps_service.company_merge.similarity import cosine_similarity
from ps_service.domain_mapper.identity import capability_id, policy_id
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
    "policy_id",
    "read_existing_canonical_index",
    "resolve_capability_convergence_offline",
    "resolve_exact_match",
]

# label -> the property holding this canonical kind's own text (issue #54,
# S4): Capability's own text property is `name`, Policy's is `title` (per
# `ps-domain-concepts.md`'s attribute list for each). Never adapter/
# LLM-sourced -- a fixed lookup table keyed by this module's own Literal
# values.
_TEXT_PROPERTY_BY_LABEL: dict[Literal["Capability", "Policy"], str] = {
    "Capability": "name",
    "Policy": "title",
}


def read_existing_canonical_index(
    single_tenant_graph: GraphHandle, label: Literal["Capability", "Policy"]
) -> tuple[ExistingCanonicalNode, ...]:
    """Read every existing `label` node in the single-tenant graph (PLAN_REVIEWED.md §5.2).

    Returned as an `ExistingCanonicalNode` tuple. `label` is this module's
    own fixed literal (`"Capability"` or, since issue #54's S4, `"Policy"`),
    passed only by `dedupe_canonical_nodes` -- never sourced from an
    adapter/LLM/external input -- so it is interpolated directly into the
    query string, mirroring `graph_reader.py`'s own "fixed literal, no
    allow-list needed" precedent for its own per-relationship-type queries.
    The text property read back onto `ExistingCanonicalNode.text` is
    `label`-dependent (`_TEXT_PROPERTY_BY_LABEL`): `n.name` for Capability,
    `n.title` for Policy.

    `n.embedding` is a cached `list[float]` property once computed
    (PLAN_REVIEWED.md §5.5) -- `None`/absent for a canonical node whose
    embedding has never been computed, which this function preserves as
    `None` (never `()` or a crash) on `ExistingCanonicalNode.embedding`. An
    empty graph (no nodes of this label) returns an empty tuple, no
    exception.
    """
    text_property = _TEXT_PROPERTY_BY_LABEL[label]
    result = single_tenant_graph.query(
        f"MATCH (n:{label}) RETURN n.id, n.{text_property}, n.embedding"
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
    """Exact-key match (PLAN_REVIEWED.md §5.1): is `incoming_id` already a canonical node id?

    Domain Mapper already computed every baseline Capability node's id via
    `capability_id`, so the incoming node's own `id` field already equals its
    canonical id -- this is nothing more than a membership check against the
    single-tenant graph's existing ids.
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
) -> SemanticMatchResult | None:
    """Semantic match (PLAN_REVIEWED.md §5.3, B2's fix, Increment 8).

    Returns `None` when `existing_index` is empty -- nothing to compare
    against, so this incoming node is a first-time mint by construction, and
    ZERO `route_embedding` calls are made (not even for `incoming_text`).

    Otherwise `incoming_text`'s embedding is computed once via
    `route_embedding`. For every `existing_index` entry whose `embedding` is
    `None`, its embedding is computed too via a further `route_embedding`
    call for `entry.text`; every entry that already carries a non-`None`
    `embedding` is reused as-is, with NO call made for it.
    `similarity.cosine_similarity` scores every entry (using either its
    cached or freshly-computed embedding) against the incoming embedding.

    Two maximum-scoring entries are returned, computed from that single
    scan at zero extra `route_embedding` cost (issue #30): the overall
    maximum across every entry scanned
    (`SemanticMatchResult.best_existing_id`/`best_similarity`, always
    populated, used only for near-miss citation), and the maximum restricted
    to `eligible_ids` (`SemanticMatchResult.best_eligible_id`/
    `best_eligible_similarity`, used only for the merge decision).
    `eligible_ids=None` (the default) treats every scanned entry as
    eligible, making `best_eligible_id`/`best_eligible_similarity` identical
    to the overall best -- fully backward compatible with every pre-issue-#30
    call site. `eligible_ids=frozenset()` (or a set matching none of the
    scanned entries) yields `best_eligible_id=None`,
    `best_eligible_similarity=None`. Neither pair applies a similarity
    threshold: the caller (`dedupe_canonical_nodes`) decides merge-vs-surface
    by comparing `best_eligible_similarity` against a threshold, not this
    function.

    Every existing entry that needed a fresh embedding this call -- not just
    the eventual best match -- is returned via
    `SemanticMatchResult.newly_computed_existing_embeddings` (`existing_id ->
    embedding`), so a caller can both reuse it in-memory for the rest of a
    dedup run AND eventually persist it via
    `graph_writer.backfill_canonical_embeddings`. This return-value plumbing
    is the entire point of B2's fix: the prior design only ever returned the
    incoming node's own embedding, discarding everything computed for an
    existing candidate during the scan.

    A `LlmProviderError` from any `route_embedding` call propagates
    unchanged -- no try/except in this function.
    """
    if not existing_index:
        return None

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


def _merge_target_id(result: SemanticMatchResult | None, threshold: float) -> str | None:
    """The eligible canonical id to merge onto, or `None` to mint instead (issue #30).

    `None` when there is no semantic-match result at all, no scanned entry
    belonged to `eligible_ids` (`result.best_eligible_id`/
    `best_eligible_similarity` both `None`), or the eligible candidate's
    score does not clear `threshold` -- the caller mints a new canonical
    node in every such case. Otherwise, the eligible candidate's id.
    """
    if (
        result is None
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
    """AC-BI-004 (merge-branch case, CHANGES.md row 1).

    Called only from the merge branch, where `result.best_eligible_id` just
    clearing `threshold` produced a merge. If the overall best-scoring
    candidate across the FULL working index (`result.best_existing_id`)
    differs from the eligible candidate just merged onto, and itself clears
    `threshold`, it can only be a same-run mint: an eligible entry achieving
    the global max score is necessarily the max over the eligible-filtered
    subset too, so if the two ids differ, the overall best is NOT in
    `eligible_ids`, i.e. it was minted this run. Appends a `NearMissPair`
    citing it onto `near_misses` in that case; a no-op otherwise. This is
    recorded even though a merge also happened -- the mint branch's own
    near-miss construction never runs on this path, so this is the only
    place this case can be recorded, and the two can never double up
    (mint/merge are mutually exclusive per node).
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


def _incoming_text(node: BaselineNode, kind: Literal["Capability", "Policy"]) -> str:
    """Return an incoming node's own text, dispatched on `kind` (issue #54, S4).

    `properties["name"]` for Capability, `properties["title"]` for Policy --
    mirrors `graph_reader.read_baseline_graph`'s own property-key convention
    (see `test_graph_reader.py`'s fixtures) and
    `read_existing_canonical_index`'s own `_TEXT_PROPERTY_BY_LABEL`
    dispatch, kept as two separate lookup tables since one reads a
    `BaselineNode.properties` dict and the other a query's property name.
    """
    key = _TEXT_PROPERTY_BY_LABEL[kind]
    return cast("str", node.properties[key])


def dedupe_canonical_nodes(
    incoming_nodes: tuple[BaselineNode, ...],
    *,
    kind: Literal["Capability", "Policy"],
    single_tenant_graph: GraphHandle,
    model: str,
    threshold: float,
    call_embedding: EmbeddingCaller | None = None,
    emitter: LogEmitter | None = None,
) -> DedupResult:
    """Combined resolution over the whole incoming collection, before any write.

    PLAN_REVIEWED.md §5.4, Increment 9 -- run for the WHOLE incoming
    collection before `merge.py` writes anything. `kind` is `"Capability"`
    since #42 (Obligation is passed through, not deduped) or, since issue
    #54's S4, `"Policy"` (Standard/Control are weak entities, passed
    through, never deduped); it is passed straight to
    `read_existing_canonical_index` as its `label`.

    Makes exactly one read call (`read_existing_canonical_index`) and never
    a single write call -- "abort with no partial write" on a
    `LlmProviderError` from `find_best_semantic_match` is therefore
    automatically satisfied by construction, not by any try/except here.

    For each incoming node, in order: exact-key match first (against the
    working index, which grows as nodes are minted/matched onto within this
    same run -- same-run EXACT convergence is unaffected by issue #30);
    else a semantic match is attempted, but only nodes present in the
    ORIGINAL, pre-run existing index (`original_existing_ids`) are eligible
    merge targets. This resolves the question issue #16 left open
    (`.orchestrator/tracker/issue-16-company-merge/PLAN_REVIEWED.md` §13
    item 6, "whether the in-run convergence property is desired") as NO: a
    same-run near-duplicate is never itself a merge target, it is only ever
    recorded as a `NearMissPair` (see `_merge_target_id` and
    `_record_excluded_same_run_mint_near_miss`). Any existing entry whose
    embedding had to be freshly computed during that call is folded into
    the working index immediately (B2's within-run reuse fix) and, if it
    was present in the ORIGINAL existing index (i.e. genuinely
    pre-existing, not minted this run), recorded into `embedding_backfills`
    for `graph_writer.backfill_canonical_embeddings` to persist later. A
    `None` result, no eligible candidate, or a below-threshold eligible
    best score mints a new canonical node (recording a `NearMissPair`
    whenever any score was computed at all, eligible or not); an
    at-or-above-threshold eligible best score resolves onto that existing
    canonical id, and may itself still record a second `NearMissPair` when
    an ineligible same-run mint scored even higher
    (`_record_excluded_same_run_mint_near_miss`).
    """
    existing_index = read_existing_canonical_index(single_tenant_graph, kind)
    # Fixed at the start, never mutated -- distinguishes a genuinely
    # pre-existing canonical node (a backfill candidate) from one minted
    # later in this same run.
    original_existing_ids = frozenset(n.id for n in existing_index)
    working_index: dict[str, ExistingCanonicalNode] = {n.id: n for n in existing_index}
    embedding_backfills: dict[str, tuple[float, ...]] = {}
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
            eligible_ids=original_existing_ids,
        )

        if result is not None:
            # B2's within-run reuse fix: fold every freshly-computed
            # existing embedding into the working index immediately, so a
            # later incoming node comparing against the same entry makes
            # zero further embedding calls for it.
            for existing_id, embedding in result.newly_computed_existing_embeddings.items():
                working_index[existing_id] = ExistingCanonicalNode(
                    id=existing_id,
                    text=working_index[existing_id].text,
                    embedding=embedding,
                )
                if existing_id in original_existing_ids:
                    embedding_backfills[existing_id] = embedding

        merge_target_id = _merge_target_id(result, threshold)

        if merge_target_id is None:
            own_embedding = result.incoming_embedding if result is not None else None
            resolutions.append(
                CanonicalResolution(
                    incoming_id=node.id,
                    canonical_id=node.id,
                    match_kind="new",
                    embedding=own_embedding,
                )
            )
            # Same-run EXACT convergence remains (via resolve_exact_match's
            # unrestricted pool, unchanged above); same-run SEMANTIC
            # convergence is deliberately excluded as of issue #30 -- the
            # semantic-match candidate pool passed to find_best_semantic_match
            # above is restricted to ORIGINAL, pre-run existing nodes only
            # (eligible_ids=original_existing_ids), so a same-run mint can
            # never itself be an eligible merge target. This newly-minted
            # node is still reflected in the working index immediately so a
            # later incoming node's EXACT-match check can still find it.
            working_index[node.id] = ExistingCanonicalNode(
                id=node.id, text=node_text, embedding=own_embedding
            )
            if result is not None:
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
            # `_merge_target_id` only ever returns non-None when `result` is
            # not None (see its docstring) -- `result` is narrowed here via
            # cast rather than a redundant `is not None` check.
            _record_excluded_same_run_mint_near_miss(
                node,
                node_text,
                cast("SemanticMatchResult", result),
                working_index,
                threshold,
                near_misses,
            )

    return DedupResult(
        resolutions=tuple(resolutions),
        near_misses=tuple(near_misses),
        embedding_backfills=embedding_backfills,
    )


@dataclass(frozen=True, slots=True)
class _OfflineMatchResult:
    """`_best_offline_match`'s return value -- offline twin of `SemanticMatchResult` (issue #30).

    `best_eligible`/`best_eligible_score`: the best-scoring scorable
    candidate restricted to `eligible_ids`, used only for the merge decision
    (AC-BI-003) -- both `None`/`0.0` when no scorable candidate belongs to
    `eligible_ids`. `best_overall`/`best_overall_score`: the best-scoring
    scorable candidate across EVERY entry scanned, regardless of
    eligibility, used only for near-miss citation. Both pairs are computed
    from the one `scorable` scan, at zero extra cost.
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
    """Score `node`'s own (artifact-supplied) embedding against every scorable candidate.

    A candidate is scorable only if it already carries a cached embedding
    (D6's "skip, don't fetch" -- a candidate with no cached embedding is
    excluded, never fetched). Returns both the overall best-scoring scorable
    candidate and the best-scoring candidate restricted to `eligible_ids`
    (issue #30, AC-BI-003) -- see `_OfflineMatchResult`. Every field is
    `None`/`0.0` when `own_embedding` is `None` or no candidate is scorable;
    `best_eligible`/`best_eligible_score` alone are `None`/`0.0` when no
    scorable candidate belongs to `eligible_ids`. The caller mints a new
    canonical node whenever `best_eligible` is `None`, exactly as an empty
    `working_index` would.
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
    """AC-BI-004, offline twin of `_record_excluded_same_run_mint_near_miss` (CHANGES.md row 1).

    Called only from the merge branch, where `best_eligible` (the candidate
    just merged onto) already cleared `threshold`. If the overall
    best-scoring scorable candidate (`match.best_overall`) differs from
    `best_eligible`, and itself clears `threshold`, it can only be a
    same-run mint: an eligible entry achieving the global max score is
    necessarily the max over the eligible-filtered subset too, so if the two
    differ, `match.best_overall` was not in `eligible_ids`, i.e. it was
    minted this run (identical reasoning to the live path's twin -- see its
    docstring for the full proof). Appends a `NearMissPair` citing it onto
    `near_misses` in that case; a no-op otherwise -- mint/merge are mutually
    exclusive per node, so this can never double up with the mint branch's
    own near-miss construction.
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
    threshold: float,
    kind: Literal["Capability", "Policy"] = "Capability",
    emitter: LogEmitter | None = None,
) -> DedupResult:
    """D6's offline counterpart to `dedupe_canonical_nodes`, for a restore's baseline merge.

    Structurally mirrors `dedupe_canonical_nodes`'s own working-index growth
    (CHANGES.md MA1): processes the WHOLE `incoming_nodes` batch in one
    call, exact-match first (via `resolve_exact_match`, reused verbatim),
    then a semantic match scored via `cosine_similarity` alone against
    `incoming_embeddings`'s artifact-supplied vectors and the existing
    index's own cached embeddings -- never `route_embedding`, never an
    `EmbeddingCaller` (a restore has no live LLM provider to call; every
    embedding it can ever use was already computed at export time). An
    existing candidate with no cached embedding is excluded from scoring
    entirely, not fetched; a newly-minted node is folded into the working
    index immediately, so a later incoming node in this same batch
    converges onto it instead of minting a separate node -- the same in-run
    EXACT-convergence mechanism `dedupe_canonical_nodes` already has (its
    own working-index fold-in inside the mint branch, `dedup.py:394-396`,
    feeding its own exact-match check, `dedup.py:337-339`; same-run
    SEMANTIC convergence is excluded on both paths, issue #30). When one or
    more existing candidates were skipped for lacking a cached embedding,
    one aggregate `outcome="warning"` log entry records the total count
    (OQ4).

    `kind` defaults to `"Capability"` (its original, pre-#54 scope) and
    widens to `"Policy"` (issue #54, S6/B6) -- dispatched into
    `read_existing_canonical_index`/`_incoming_text` exactly as
    `dedupe_canonical_nodes` already dispatches its own `kind` parameter for
    the live path (`_TEXT_PROPERTY_BY_LABEL`), so a restore's offline Policy
    convergence uses the identical text-property/read-query mapping.
    """
    existing_index = read_existing_canonical_index(single_tenant_graph, kind)
    # Fixed at the start, never mutated -- mirrors dedupe_canonical_nodes's
    # own original_existing_ids snapshot (issue #30, AC-BI-003): distinguishes
    # a genuinely pre-existing canonical node from one minted later in this
    # same restore run.
    original_existing_ids = frozenset(n.id for n in existing_index)
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
        match = _best_offline_match(own_embedding, working_index, original_existing_ids)
        best_eligible = match.best_eligible

        if best_eligible is None or match.best_eligible_score < threshold:
            resolutions.append(
                CanonicalResolution(
                    incoming_id=node.id,
                    canonical_id=node.id,
                    match_kind="new",
                    embedding=own_embedding,
                )
            )
            if match.best_overall is not None:
                near_misses.append(
                    NearMissPair(
                        incoming_id=node.id,
                        incoming_text=node_text,
                        nearest_existing_id=match.best_overall.id,
                        nearest_existing_text=match.best_overall.text,
                        similarity=match.best_overall_score,
                    )
                )
            # Same-run EXACT convergence remains (resolve_exact_match's
            # unrestricted pool, unchanged above); same-run SEMANTIC
            # convergence is deliberately excluded as of issue #30
            # (AC-BI-003) -- the eligible-candidate pool passed to
            # _best_offline_match above is restricted to ORIGINAL, pre-run
            # existing nodes only, so a same-run mint can never itself be an
            # eligible merge target. This newly-minted node is still
            # reflected in the working index immediately so a LATER node in
            # this SAME artifact can still find it via EXACT match.
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
                node, node_text, match, best_eligible, threshold, near_misses
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
