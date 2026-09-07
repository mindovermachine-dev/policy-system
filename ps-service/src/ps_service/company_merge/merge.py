"""`merge_baseline_graph` -- the `MergeBaselineGraph` public action.

The top-level orchestration (PLAN_REVIEWED.md §7, §10 Increments 13-14) that
wires `graph_reader.read_baseline_graph`, `dedup.dedupe_canonical_nodes`
(Capability, and since issue #54's S4, Policy), and every `graph_writer`
function together.

Flow (§7, extended by #54 S4's item 3a):

0. B1's fix, enforced first, before anything else runs: if
   `similarity_threshold is None`, raise `CompanyMergeConfigurationError` --
   zero graph calls of any kind have been made by this point, not even
   `graph_reader.read_baseline_graph`.
1. Read the regulation's `{short}_baseline` graph.
2. Dedupe Capability nodes against the single-tenant graph. (Obligation is
   Role-scoped since #42 -- a weak entity of exactly one Role, never deduped
   across sources -- so there is no Obligation dedup pass.)
3. Persist RegulatoryInstrument/Role/Requirement/`DEFINES`/`EXPRESSES` and
   Obligation (unconditional `SET`); persist canonical Capability nodes for
   every `match_kind="new"` resolution (`ON CREATE SET`).
3a. (#54, S4) If `graph.policy_nodes` is non-empty (an internal-sourced
    baseline): dedupe Policy nodes the same way Capability was deduped
    above, persist canonical Policy nodes, and persist Standard/Control as
    unconditional-`SET` passthrough nodes (weak entities, never deduped).
    An external-sourced baseline (`graph.policy_nodes == ()`) skips this
    step entirely -- a structural no-op.
4. Persist `HAS`/`SATISFIED_BY`/`REQUIRES`/`GOVERNED_BY`/`SUPPORTED_BY`/
   `IMPLEMENTED_BY` edges in one `persist_rewired_edges` call, using the
   combined Capability+Policy canonical-id mapping (only a `REQUIRES`
   edge's Capability target and a `GOVERNED_BY` edge's Policy target are
   ever rewritten -- see `graph_writer.persist_rewired_edges`); then
   backfill Capability (and, if step 3a ran, Policy) embeddings.
5. Emit one `outcome="succeeded"` entry for the whole call. No
   `bind_run_context()` self-bind here -- `run_id` is whatever the caller
   already bound, or `None`.
6. Emit one additional log entry per Capability (and Policy) dedup decision
   (AC-007): one per `CanonicalResolution`, outcome=`match_kind`; one per
   `NearMissPair`, outcome="near_miss".
7. Return `MergeResult`.

If step 2 (or 3a's Policy dedup) raises (`LlmProviderError` from an
embedding call), the exception propagates unchanged. For step 2, no write
has yet occurred at all, satisfying "abort with no partial write" as a
structural property of call order. For 3a's Policy dedup, per PLAN.md §6 S4
this pass deliberately runs AFTER the Capability writes already landed --
so a Policy-dedup failure after step 3 leaves the Capability writes in
place; `dedupe_canonical_nodes` itself never issues a write call of its own
either way (see `dedup.py`'s own docstring).

`DedupeCanonicalNodes` is not exposed as its own separately-invocable public
action alongside `merge_baseline_graph`, per the CA doc's exact wording
(PLAN_REVIEWED.md §0.1) -- there is no standalone function a caller invokes
for it; it only ever runs as part of this orchestration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.company_merge import dedup, graph_reader, graph_writer
from ps_service.company_merge.errors import CompanyMergeConfigurationError
from ps_service.company_merge.models import MergeResult
from ps_service.logging import LogEmitter, emit_log_entry

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.models import BaselineGraph, DedupResult
    from ps_service.llm_interface.client import EmbeddingCaller

__all__ = ["merge_baseline_graph"]

_COMPONENT = "company_merge"
_MERGE_ACTION = "merge_baseline_graph"
_DEDUP_ACTION = "dedupe_canonical_nodes"


def _run_policy_pass(
    graph: BaselineGraph,
    *,
    single_tenant_graph: GraphHandle,
    embed_model: str,
    similarity_threshold: float,
    call_embedding: EmbeddingCaller | None,
    emitter: LogEmitter | None,
    canonical_id_by_incoming_id: dict[str, str],
) -> DedupResult | None:
    """Issue #54, S4's Policy convergence + Standard/Control passthrough pass.

    A no-op (returns `None`, no calls of any kind) when `graph.policy_nodes`
    is empty -- an external-sourced baseline. Otherwise: dedupe Policy nodes
    the same way Capability was deduped by the caller, persist canonical
    Policy nodes, persist Standard/Control as unconditional-`SET` passthrough
    nodes (weak entities, never deduped), and fold the Policy resolutions
    into `canonical_id_by_incoming_id` (mutated in place) so the caller's
    single `persist_rewired_edges` call covers both Capability and Policy
    endpoints. Extracted from `merge_baseline_graph` to keep its own
    cyclomatic complexity within L1's budget.
    """
    if not graph.policy_nodes:
        return None

    policy_dedup = dedup.dedupe_canonical_nodes(
        graph.policy_nodes,
        kind="Policy",
        single_tenant_graph=single_tenant_graph,
        model=embed_model,
        threshold=similarity_threshold,
        call_embedding=call_embedding,
        emitter=emitter,
    )
    graph_writer.persist_canonical_nodes(
        single_tenant_graph,
        graph.policy_nodes,
        policy_dedup.resolutions,
        kind="Policy",
    )
    graph_writer.persist_standard_and_control_passthrough(
        single_tenant_graph, graph.standard_nodes, graph.control_nodes
    )
    canonical_id_by_incoming_id.update(
        {resolution.incoming_id: resolution.canonical_id for resolution in policy_dedup.resolutions}
    )
    return policy_dedup


def _log_dedup_decisions(dedup_result: DedupResult | None, *, emitter: LogEmitter | None) -> None:
    """Emit one log entry per `CanonicalResolution` and per `NearMissPair` (AC-007).

    A no-op when `dedup_result` is `None` -- the Policy pass never ran
    (external-sourced baseline). Shared between the Capability and Policy
    passes so `merge_baseline_graph` itself carries no per-kind branching for
    this step.
    """
    if dedup_result is None:
        return
    for resolution in dedup_result.resolutions:
        emit_log_entry(
            component=_COMPONENT,
            action=_DEDUP_ACTION,
            entity_id=resolution.incoming_id,
            outcome=resolution.match_kind,
            emitter=emitter,
        )
    for near_miss in dedup_result.near_misses:
        emit_log_entry(
            component=_COMPONENT,
            action=_DEDUP_ACTION,
            entity_id=near_miss.incoming_id,
            outcome="near_miss",
            emitter=emitter,
        )


def _finish_policy_pass(
    single_tenant_graph: GraphHandle,
    policy_dedup: DedupResult | None,
    *,
    emitter: LogEmitter | None,
) -> None:
    """Backfill Policy embeddings and log Policy dedup decisions; a no-op if no Policy pass ran."""
    if policy_dedup is None:
        return
    graph_writer.backfill_canonical_embeddings(
        single_tenant_graph,
        kind="Policy",
        embeddings=policy_dedup.embedding_backfills,
    )
    _log_dedup_decisions(policy_dedup, emitter=emitter)


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
    """Merge one regulation's `{short}_baseline` graph into `single_tenant_graph`.

    `MergeBaselineGraph`, PLAN_REVIEWED.md §7. `similarity_threshold` is
    required at this call site (B1's fix, PLAN_REVIEWED.md §8): `None` means
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
    # target (and, since issue #54's S4, a GOVERNED_BY edge's Policy
    # target) -- Obligation/Standard/Control are passthrough nodes, so the
    # mapping carries Capability (and, below, Policy) resolutions alone.
    canonical_id_by_incoming_id: dict[str, str] = {
        resolution.incoming_id: resolution.canonical_id
        for resolution in capability_dedup.resolutions
    }

    # issue #54, S4 -- the Policy convergence + Standard/Control passthrough
    # pass, a structural no-op for an external-sourced baseline (empty
    # policy_nodes): no Policy dedup read, no Standard/Control write, no
    # Policy entries folded into the rewiring mapping. Mirrors the
    # Capability pass above exactly; extracted to `_run_policy_pass` to keep
    # this function's own cyclomatic complexity within budget.
    policy_dedup = _run_policy_pass(
        graph,
        single_tenant_graph=single_tenant_graph,
        embed_model=embed_model,
        similarity_threshold=similarity_threshold,
        call_embedding=call_embedding,
        emitter=emitter,
        canonical_id_by_incoming_id=canonical_id_by_incoming_id,
    )

    # One rewiring call over BOTH the regulatory-spine edges and the
    # governance edges -- governance_edges is empty for an external
    # baseline, so this is unchanged from before S4 in that case.
    graph_writer.persist_rewired_edges(
        single_tenant_graph,
        graph.bare_edges + graph.governance_edges,
        canonical_id_by_incoming_id,
    )

    graph_writer.backfill_canonical_embeddings(
        single_tenant_graph,
        kind="Capability",
        embeddings=capability_dedup.embedding_backfills,
    )
    _finish_policy_pass(single_tenant_graph, policy_dedup, emitter=emitter)

    emit_log_entry(
        component=_COMPONENT,
        action=_MERGE_ACTION,
        entity_id=regulatory_instrument_id,
        outcome="succeeded",
        emitter=emitter,
    )

    _log_dedup_decisions(capability_dedup, emitter=emitter)

    return MergeResult(
        regulatory_instrument_id=regulatory_instrument_id,
        obligation_ids=tuple(node.id for node in graph.obligation_nodes),
        capability_canonical_ids=tuple(r.canonical_id for r in capability_dedup.resolutions),
        near_misses=capability_dedup.near_misses,
        policy_canonical_ids=(
            tuple(r.canonical_id for r in policy_dedup.resolutions)
            if policy_dedup is not None
            else ()
        ),
    )
