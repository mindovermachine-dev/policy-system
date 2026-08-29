"""ps_service.company_merge.dedup -- exact-key identity reuse (PLAN_REVIEWED.md §3)
plus the existing-canonical-index reader, exact-match resolution,
semantic-match resolution, and the combined whole-collection resolution
algorithm (PLAN_REVIEWED.md §5.1/§5.2/§5.3/§5.4, Increments 6-9).

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

from ps_service.company_merge.falkordb_client import GraphHandle
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
from ps_service.llm_interface.client import EmbeddingCaller
from ps_service.llm_interface.embedding import route_embedding
from ps_service.logging.emitter import LogEmitter

__all__ = [
    "capability_id",
    "dedupe_canonical_nodes",
    "find_best_semantic_match",
    "read_existing_canonical_index",
    "resolve_exact_match",
]

_CAPABILITY_INDEX_QUERY = "MATCH (n:Capability) RETURN n.id, n.name, n.embedding"


def read_existing_canonical_index(
    single_tenant_graph: GraphHandle, label: Literal["Capability"]
) -> tuple[ExistingCanonicalNode, ...]:
    """Read every existing `label` node already present in the single-tenant
    graph, as an `ExistingCanonicalNode` tuple (PLAN_REVIEWED.md §5.2).

    `label` is always this module's own fixed literal (`"Capability"`),
    passed only by this component's own code (`dedupe_canonical_nodes`) --
    never sourced from an adapter/LLM/external input -- so it is
    interpolated directly into the query string, mirroring
    `graph_reader.py`'s own "fixed literal, no allow-list needed" precedent
    for its own per-relationship-type queries. The parameter is kept for
    symmetry with a future internal-SoP Policy pass.

    `n.embedding` is a cached `list[float]` property once computed
    (PLAN_REVIEWED.md §5.5) -- `None`/absent for a canonical node whose
    embedding has never been computed, which this function preserves as
    `None` (never `()` or a crash) on `ExistingCanonicalNode.embedding`. An
    empty graph (no nodes of this label) returns an empty tuple, no
    exception.
    """
    result = single_tenant_graph.query(_CAPABILITY_INDEX_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    nodes: list[ExistingCanonicalNode] = []
    for row in rows:
        node_id, text, embedding = row
        raw_embedding = cast("list[float] | None", embedding)
        nodes.append(
            ExistingCanonicalNode(
                id=cast(str, node_id),
                text=cast(str, text),
                embedding=tuple(raw_embedding) if raw_embedding is not None else None,
            )
        )
    return tuple(nodes)


def resolve_exact_match(incoming_id: str, existing_ids: frozenset[str]) -> bool:
    """Exact-key match (PLAN_REVIEWED.md §5.1): does `incoming_id` already
    exist as a canonical node id in the single-tenant graph? Domain Mapper
    already computed every baseline Capability node's id via `capability_id`,
    so the incoming node's own `id` field already equals its canonical id --
    this is nothing more than a membership check."""
    return incoming_id in existing_ids


def find_best_semantic_match(
    incoming_text: str,
    existing_index: tuple[ExistingCanonicalNode, ...],
    *,
    model: str,
    threshold: float,
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
    the maximum-scoring entry is returned regardless of whether it clears
    `threshold` -- the caller (`dedupe_canonical_nodes`, a later increment)
    decides merge-vs-surface, not this function.

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
    unchanged -- no try/except in this function. `threshold` is accepted
    here only so callers have a single, stable signature to call through
    regardless of which layer ends up applying it; this function itself
    never consults it -- the merge-vs-surface decision belongs entirely to
    the caller (`dedupe_canonical_nodes`)."""
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
    """An incoming Capability's name lives under `properties["name"]` --
    mirrors `graph_reader.read_baseline_graph`'s own property-key convention
    (see `test_graph_reader.py`'s fixtures)."""
    return cast(str, node.properties["name"])


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
    """Combined resolution, whole-collection, before any write
    (PLAN_REVIEWED.md §5.4, Increment 9) -- run for the WHOLE incoming
    Capability collection before `merge.py` writes anything. `kind` is
    `"Capability"` only since #42 (Obligation is passed through, not
    deduped); the parameter is kept for a future internal-SoP Policy pass.

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
            threshold=threshold,
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
