"""`merge_baseline_graph` -- the `MergeBaselineGraph` public action
(PLAN_REVIEWED.md §7, §10 Increments 13-14): the top-level orchestration that
wires `graph_reader.read_baseline_graph`, `dedup.dedupe_canonical_nodes`
(Capability only since #42), and every `graph_writer` function together.

Flow (§7):

0. B1's fix, enforced first, before anything else runs: if
   `similarity_threshold is None`, raise `CompanyMergeConfigurationError` --
   zero graph calls of any kind have been made by this point, not even
   `graph_reader.read_baseline_graph`.
1. Read the regulation's `{short}_baseline` graph.
2. Dedupe Capability nodes against the single-tenant graph. (Obligation is
   Role-scoped since #42 -- a weak entity of exactly one Role, never deduped
   across sources -- so there is no Obligation dedup pass.)
3. Only now, having completed the dedup pass with no exception: persist
   RegulatoryInstrument/Role/Requirement/`DEFINES`/`EXPRESSES` and Obligation
   (unconditional `SET`); persist canonical Capability nodes for every
   `match_kind="new"` resolution (`ON CREATE SET`); persist
   `HAS`/`SATISFIED_BY`/`REQUIRES` edges using the Capability canonical-id
   mapping (only a `REQUIRES` edge's Capability target is ever rewritten --
   see `graph_writer.persist_rewired_edges`); then backfill Capability
   embeddings.
4. Emit one `outcome="succeeded"` entry for the whole call. No
   `bind_run_context()` self-bind here -- `run_id` is whatever the caller
   already bound, or `None`.
5. Emit one additional log entry per Capability dedup decision (AC-007): one
   per `CanonicalResolution`, outcome=`match_kind`; one per `NearMissPair`,
   outcome="near_miss".
6. Return `MergeResult`.

If step 2 raises (`LlmProviderError` from an embedding call), the exception
propagates unchanged -- no entries from step 3 have been written, satisfying
"abort with no partial write" as a structural property of call order:
`dedupe_canonical_nodes` never issues a write call of its own (see
`dedup.py`'s own docstring).

`DedupeCanonicalNodes` is not exposed as its own separately-invocable public
action alongside `merge_baseline_graph`, per the CA doc's exact wording
(PLAN_REVIEWED.md §0.1) -- there is no standalone function a caller invokes
for it; it only ever runs as part of this orchestration.
"""

from __future__ import annotations

from ps_service.company_merge import dedup, graph_reader, graph_writer
from ps_service.company_merge.errors import CompanyMergeConfigurationError
from ps_service.company_merge.falkordb_client import GraphHandle
from ps_service.company_merge.models import MergeResult
from ps_service.llm_interface.client import EmbeddingCaller
from ps_service.logging import LogEmitter, emit_log_entry

__all__ = ["merge_baseline_graph"]

_COMPONENT = "company_merge"
_MERGE_ACTION = "merge_baseline_graph"
_DEDUP_ACTION = "dedupe_canonical_nodes"


def merge_baseline_graph(
    regulatory_instrument_id: str,
    *,
    baseline_graph: GraphHandle,
    single_tenant_graph: GraphHandle,
    embed_model: str,
    similarity_threshold: float | None,
    call_embedding: EmbeddingCaller | None = None,
    emitter: LogEmitter | None = None,
) -> MergeResult:
    """Merge one regulation's `{short}_baseline` graph into
    `single_tenant_graph` (`MergeBaselineGraph`, PLAN_REVIEWED.md §7).

    `similarity_threshold` is required at this call site (B1's fix,
    PLAN_REVIEWED.md §8): `None` means
    `PS_COMPANYMERGE_SIMILARITY_THRESHOLD` was never resolved via
    `ServiceConfig`, and raises `CompanyMergeConfigurationError` immediately
    -- before `baseline_graph`/`single_tenant_graph` receive a single call of
    any kind.

    Add/merge-only (per UC-1): an existing canonical Obligation/Capability
    node's own properties are never overwritten -- only its incoming
    duplicate's edges are rewired onto it. See `graph_writer.py` for the
    `ON CREATE SET`/`WHERE n.embedding IS NULL` mechanisms that make this a
    database-engine guarantee, not application-logic discipline.
    """
    if similarity_threshold is None:
        raise CompanyMergeConfigurationError(
            "similarity_threshold is required -- no default is defined; resolve "
            "PS_COMPANYMERGE_SIMILARITY_THRESHOLD via ServiceConfig before calling "
            "merge_baseline_graph"
        )

    graph = graph_reader.read_baseline_graph(baseline_graph, regulatory_instrument_id)

    capability_dedup = dedup.dedupe_canonical_nodes(
        graph.capability_nodes,
        kind="Capability",
        single_tenant_graph=single_tenant_graph,
        model=embed_model,
        threshold=similarity_threshold,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    # Only now, having completed the dedup pass with no exception, is
    # anything written -- "abort with no partial write" on a raised
    # LlmProviderError is therefore automatic, not enforced by a try/except.
    graph_writer.persist_role_and_requirement_passthrough(
        single_tenant_graph,
        regulatory_instrument_id,
        graph.regulatory_instrument_properties,
        graph.role_nodes,
        graph.requirement_nodes,
        graph.provenance_edges,
    )
    graph_writer.persist_obligation_passthrough(single_tenant_graph, graph.obligation_nodes)
    graph_writer.persist_canonical_nodes(
        single_tenant_graph,
        graph.capability_nodes,
        capability_dedup.resolutions,
        kind="Capability",
    )

    # persist_rewired_edges only ever rewrites a REQUIRES edge's Capability
    # target -- Obligation is a passthrough node since #42, so the mapping
    # carries Capability resolutions alone.
    canonical_id_by_incoming_id: dict[str, str] = {
        resolution.incoming_id: resolution.canonical_id
        for resolution in capability_dedup.resolutions
    }
    graph_writer.persist_rewired_edges(
        single_tenant_graph, graph.bare_edges, canonical_id_by_incoming_id
    )

    graph_writer.backfill_canonical_embeddings(
        single_tenant_graph,
        kind="Capability",
        embeddings=capability_dedup.embedding_backfills,
    )

    emit_log_entry(
        component=_COMPONENT,
        action=_MERGE_ACTION,
        entity_id=regulatory_instrument_id,
        outcome="succeeded",
        emitter=emitter,
    )

    for resolution in capability_dedup.resolutions:
        emit_log_entry(
            component=_COMPONENT,
            action=_DEDUP_ACTION,
            entity_id=resolution.incoming_id,
            outcome=resolution.match_kind,
            emitter=emitter,
        )
    for near_miss in capability_dedup.near_misses:
        emit_log_entry(
            component=_COMPONENT,
            action=_DEDUP_ACTION,
            entity_id=near_miss.incoming_id,
            outcome="near_miss",
            emitter=emitter,
        )

    return MergeResult(
        regulatory_instrument_id=regulatory_instrument_id,
        obligation_ids=tuple(node.id for node in graph.obligation_nodes),
        capability_canonical_ids=tuple(r.canonical_id for r in capability_dedup.resolutions),
        near_misses=capability_dedup.near_misses,
    )
