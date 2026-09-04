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
`tests/company_merge/test_dedup_offline_no_route_embedding_import.py`).

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
    single_tenant_graph: GraphHandle, label: Literal["Capability"]
) -> tuple[ExistingCanonicalNode, ...]:
    """Read every existing `label` node in the single-tenant graph (PLAN_REVIEWED.md §5.2).

    Returned as an `ExistingCanonicalNode` tuple. `label` is this module's
    own fixed literal (`"Capability"`), passed only by `dedupe_canonical_nodes`
    -- never sourced from an adapter/LLM/external input -- so it is
    interpolated directly into the query string, mirroring `graph_reader.py`'s
    own "fixed literal, no allow-list needed" precedent for its own
    per-relationship-type queries. The parameter is also the scope hook for a
    future internal-SoP Policy pass.

    `n.embedding` is a cached `list[float]` property once computed
    (PLAN_REVIEWED.md §5.5) -- `None`/absent for a canonical node whose
    embedding has never been computed, which this function preserves as
    `None` (never `()` or a crash) on `ExistingCanonicalNode.embedding`. An
    empty graph (no nodes of this label) returns an empty tuple, no
    exception.
    """
    result = single_tenant_graph.query(f"MATCH (n:{label}) RETURN n.id, n.name, n.embedding")
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
    cached or freshly-computed embedding) against the incoming embedding;
    the maximum-scoring entry is returned regardless of its score -- the
    caller (`dedupe_canonical_nodes`) decides merge-vs-surface by comparing
    it against a similarity threshold, not this function.

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
    unchanged -- no try/except in this function. This function never applies
    a similarity threshold: it always returns the maximum-scoring entry, and
    the merge-vs-surface decision belongs entirely to the caller
    (`dedupe_canonical_nodes`).
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

    return SemanticMatchResult(
        best_existing_id=best_existing_id,
        best_similarity=best_similarity,
        incoming_embedding=incoming_embedding,
        newly_computed_existing_embeddings=newly_computed_existing_embeddings,
    )


def _incoming_name(node: BaselineNode) -> str:
    """Return an incoming Capability's name from `properties["name"]`.

    Mirrors `graph_reader.read_baseline_graph`'s own property-key convention
    (see `test_graph_reader.py`'s fixtures).
    """
    return cast("str", node.properties["name"])


def dedupe_canonical_nodes(
    incoming_nodes: tuple[BaselineNode, ...],
    *,
    kind: Literal["Capability"],
    single_tenant_graph: GraphHandle,
    model: str,
    threshold: float,
    call_embedding: EmbeddingCaller | None = None,
    emitter: LogEmitter | None = None,
) -> DedupResult:
    """Combined resolution over the whole incoming collection, before any write.

    PLAN_REVIEWED.md §5.4, Increment 9 -- run for the WHOLE incoming
    Capability collection before `merge.py` writes anything. `kind` is
    `"Capability"` only since #42 (Obligation is passed through, not deduped);
    it is passed straight to `read_existing_canonical_index` as its `label`
    and is also the scope hook for a future internal-SoP Policy pass.

    Makes exactly one read call (`read_existing_canonical_index`) and never
    a single write call -- "abort with no partial write" on a
    `LlmProviderError` from `find_best_semantic_match` is therefore
    automatically satisfied by construction, not by any try/except here.

    For each incoming node, in order: exact-key match first (against the
    working index, which grows as nodes are minted/matched onto within this
    same run -- the in-run convergence mechanism, Open Question 6); else a
    semantic match is attempted against the current working index. Any
    existing entry whose embedding had to be freshly computed during that
    call is folded into the working index immediately (B2's within-run
    reuse fix) and, if it was present in the ORIGINAL existing index (i.e.
    genuinely pre-existing, not minted this run), recorded into
    `embedding_backfills` for `graph_writer.backfill_canonical_embeddings`
    to persist later. A `None` result or a below-threshold best score mints
    a new canonical node (recording a `NearMissPair` only when a score was
    actually computed); an at-or-above-threshold best score resolves onto
    that existing canonical id.
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
        node_text = _incoming_name(node)
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

        if result is None or result.best_similarity < threshold:
            own_embedding = result.incoming_embedding if result is not None else None
            resolutions.append(
                CanonicalResolution(
                    incoming_id=node.id,
                    canonical_id=node.id,
                    match_kind="new",
                    embedding=own_embedding,
                )
            )
            # The in-run convergence mechanism (Open Question 6): reflect
            # this newly-minted node in the working index immediately, so a
            # later, semantically-equivalent incoming node in this same
            # batch converges onto it instead of minting a separate node.
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
                    canonical_id=result.best_existing_id,
                    match_kind="semantic",
                    embedding=None,
                )
            )

    return DedupResult(
        resolutions=tuple(resolutions),
        near_misses=tuple(near_misses),
        embedding_backfills=embedding_backfills,
    )


def _best_offline_match(
    own_embedding: tuple[float, ...] | None,
    working_index: dict[str, ExistingCanonicalNode],
) -> tuple[ExistingCanonicalNode | None, float]:
    """Score `node`'s own (artifact-supplied) embedding against every scorable candidate.

    A candidate is scorable only if it already carries a cached embedding
    (D6's "skip, don't fetch" -- a candidate with no cached embedding is
    excluded, never fetched). Returns `(None, 0.0)` when `own_embedding` is
    `None` or no candidate is scorable -- the caller mints a new canonical
    node in that case, exactly as an empty `working_index` would.
    """
    scorable: list[tuple[ExistingCanonicalNode, tuple[float, ...]]] = [
        (candidate, candidate.embedding)
        for candidate in working_index.values()
        if candidate.embedding is not None
    ]
    if own_embedding is None or not scorable:
        return None, 0.0
    best, best_embedding = max(scorable, key=lambda pair: cosine_similarity(own_embedding, pair[1]))
    return best, cosine_similarity(own_embedding, best_embedding)


def resolve_capability_convergence_offline(
    incoming_nodes: tuple[BaselineNode, ...],
    *,
    incoming_embeddings: dict[str, tuple[float, ...]],
    single_tenant_graph: GraphHandle,
    threshold: float,
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
    converges onto it instead of minting a separate node (the same in-run
    convergence mechanism `dedupe_canonical_nodes` already has,
    `dedup.py:222`/`:278`). When one or more existing candidates were
    skipped for lacking a cached embedding, one aggregate
    `outcome="warning"` log entry records the total count (OQ4).
    """
    existing_index = read_existing_canonical_index(single_tenant_graph, "Capability")
    working_index: dict[str, ExistingCanonicalNode] = {node.id: node for node in existing_index}
    resolutions: list[CanonicalResolution] = []
    near_misses: list[NearMissPair] = []
    skipped_count = 0

    for node in incoming_nodes:
        node_text = _incoming_name(node)
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
        best, best_score = _best_offline_match(own_embedding, working_index)

        if best is None or best_score < threshold:
            resolutions.append(
                CanonicalResolution(
                    incoming_id=node.id,
                    canonical_id=node.id,
                    match_kind="new",
                    embedding=own_embedding,
                )
            )
            if best is not None:
                near_misses.append(
                    NearMissPair(
                        incoming_id=node.id,
                        incoming_text=node_text,
                        nearest_existing_id=best.id,
                        nearest_existing_text=best.text,
                        similarity=best_score,
                    )
                )
            # MA1's fix: fold the mint into working_index immediately
            # (mirrors dedup.py:278) so a LATER node in this SAME artifact
            # converges onto it.
            working_index[node.id] = ExistingCanonicalNode(
                id=node.id, text=node_text, embedding=own_embedding
            )
        else:
            resolutions.append(
                CanonicalResolution(
                    incoming_id=node.id,
                    canonical_id=best.id,
                    match_kind="semantic",
                    embedding=None,
                )
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
