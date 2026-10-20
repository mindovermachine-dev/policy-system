"""`merge_baseline_graph` -- fold one regulation's baseline graph into the single-tenant graph.

It wires together `graph_reader.read_baseline_graph`, `redirects.resolve_redirects`,
`dedup.dedupe_canonical_nodes` (Capability, and Role review-only),
`governance_import.import_draft_governance` and the `graph_writer` functions. Every write is
submitted through the Graph Write Gateway.

Flow:

0. If either Capability threshold is `None`, or the review threshold is above the merge threshold
   (`capability_thresholds`), or the Role review threshold is `None` (`role_thresholds`), raise
   `CompanyMergeConfigurationError` before any graph call of any kind, including
   `graph_reader.read_baseline_graph`.
1. Read the regulation's `{short}_baseline` graph.
2. Drop nodes that a cleanup merge absorbed and map each to its terminal survivor
   (`redirects.resolve_redirects`); a cycle or a dangling terminal raises here, before any dedup
   embedding call and before any write.
3. Dedupe Capability nodes against the single-tenant graph. Then compare the instrument's Role
   nodes for near misses (review-only: a Role is never merged or rewired here, so Role ids are
   written exactly as read). Obligation is Role-scoped (a weak entity of exactly one Role, never
   deduped across sources), so there is no Obligation dedup pass.
4. Persist RegulatoryInstrument/Role/Requirement/`DEFINES`/`EXPRESSES` as one gateway group (a
   Role/Requirement the graph already holds is left out of the group, so it keeps its properties)
   and Obligation (create-only); persist canonical Capability nodes for every
   `match_kind="new"` resolution (create-only).
5. If the baseline carries a Policy/Standard/Control tree (an internal-sourced baseline),
   `governance_import.import_draft_governance` writes it as `draft`, owned by `governance.owner`,
   create-only and never converged onto an existing Policy. An external-sourced baseline skips this
   step; an internal one with no owner raises `GovernanceOwnerRequiredError` before any governance
   write.
6. Persist PracticeArea/RiskPath nodes as create-only passthrough, then validate that every
   `COVERS`/`OWNS`/`MITIGATED_BY`/`VERIFIED_BY` edge endpoint exists before any of those edges is
   written. There is no dedup and no embedding call for PracticeArea/RiskPath. A baseline with no
   classification-layer content skips this step.
7. Persist `HAS`/`SATISFIED_BY`/`REQUIRES`/`GOVERNED_BY`/`SUPPORTED_BY`/`IMPLEMENTED_BY`/`COVERS`/
   `OWNS`/`MITIGATED_BY`/`VERIFIED_BY` edges in one `persist_rewired_edges` call, using the
   combined Capability, Policy and redirected-node canonical-id mapping (a Capability target
   is rewritten; a Policy maps onto itself). Then backfill Capability embeddings onto the
   single-tenant graph's canonical nodes, and freshly computed incoming Capability embeddings onto
   this regulation's own `{short}_baseline` graph.
8. Emit one `outcome="succeeded"` log entry for the whole call, carrying the classification,
   governance and new/matched count fields. No `bind_run_context()` self-bind happens here:
   `run_id` is whatever the caller already bound, or `None`.
9. Emit one log entry per Capability dedup decision (one per `CanonicalResolution` with
   `outcome=match_kind`, one per `NearMissPair` with `outcome="near_miss"`), drop the near misses
   that already have an unresolved review (`drop_already_reviewed`, shared with restore), persist
   a `PendingReview` node for each remaining one and log the created / already-reviewed counts.
   Role near misses go through the same steps with kind `Role`.
10. Return `MergeResult`.

If either dedup pass raises (`LlmProviderError` from an embedding call), the exception propagates
unchanged and nothing has been written yet, because every write follows both dedup passes.

Dedup is not exposed as its own public action: `dedupe_canonical_nodes` only runs as part of this
orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from ps_service.company_merge import (
    dedup,
    governance_import,
    graph_reader,
    graph_writer,
    redirects,
)
from ps_service.company_merge.capability_config import capability_thresholds, role_thresholds
from ps_service.company_merge.generated_values import SYSTEM_VALUES
from ps_service.company_merge.governance_import import GovernanceImport, governance_count_fields
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.models import MergeResult
from ps_service.company_merge.pending_review import drop_already_reviewed, persist_pending_reviews
from ps_service.company_merge.reviewable_kinds import CAPABILITY, ROLE
from ps_service.logging import LogEmitter, emit_log_entry

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.generated_values import ValueSource
    from ps_service.company_merge.models import BaselineGraph, DedupResult, NearMissPair
    from ps_service.company_merge.reviewable_kinds import ReviewThresholds
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.llm_interface.client import EmbeddingCaller

__all__ = ["merge_baseline_graph"]

_COMPONENT = "company_merge"
_MERGE_ACTION = "merge_baseline_graph"
_DEDUP_ACTION = "dedupe_canonical_nodes"
_PERSIST_REVIEWS_ACTION = "persist_near_miss_reviews"


@dataclass(frozen=True)
class _MergeTargets:
    """The two graphs a merge run writes through the gateway, each with its own stamp state."""

    single_tenant: GraphWriteTarget
    baseline: GraphWriteTarget


def _persist_classification_passthrough(
    target: GraphWriteTarget,
    single_tenant_graph: GraphHandle,
    graph: BaselineGraph,
    canonical_id_by_incoming_id: dict[str, str],
) -> None:
    """Persist PracticeArea/RiskPath nodes and validate the classification edge endpoints.

    Identity convergence is structural (content-hashed ids), so there is no dedup and no
    embedding call. A baseline with no classification-layer content makes this a no-op:
    the passthrough reads and submits nothing for empty input, and the endpoint validation
    returns immediately when `classification_edges` is empty.

    Args:
        target: The gateway target of the single-tenant graph the nodes are written to.
        single_tenant_graph: Read handle on the single-tenant graph, used to check endpoints.
        graph: The baseline whose PracticeArea/RiskPath nodes and classification edges are used.
        canonical_id_by_incoming_id: Incoming-to-canonical id mapping, filled by the Capability
            pass and the governance import. A `COVERS`/`MITIGATED_BY` Capability target or an
            `OWNS` Policy target has an entry when dedup resolved it; every other endpoint is
            checked directly against `single_tenant_graph`, where the node passthrough has just
            persisted it.

    Raises:
        CompanyMergePersistenceError: The gateway did not take the node group, a read failed, or
            a classification edge endpoint exists nowhere (raised before any classification edge
            is written).
    """
    graph_writer.persist_practice_area_and_risk_path_passthrough(
        target, single_tenant_graph, graph.practice_area_nodes, graph.risk_path_nodes
    )
    graph_writer.validate_classification_edge_endpoints(
        single_tenant_graph, graph.classification_edges, canonical_id_by_incoming_id
    )


def _log_dedup_decisions(dedup_result: DedupResult, *, emitter: LogEmitter | None) -> None:
    """Emit one log entry per `CanonicalResolution` and per `NearMissPair` dedup decision."""
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


def _dedupe_roles(
    graph: BaselineGraph,
    *,
    regulatory_instrument_id: str,
    single_tenant_graph: GraphHandle,
    embed_model: str,
    thresholds: ReviewThresholds,
    call_embedding: EmbeddingCaller | None,
    emitter: LogEmitter | None,
) -> DedupResult:
    """Compare the baseline's Roles against the instrument's Roles; cite near pairs, merge none.

    Role is a review-only kind, so every resolution is `exact` or `new` and no Role id is
    rewritten. Runs before any write: an embedding failure leaves the graph untouched.

    Args:
        graph: The baseline whose Role nodes are compared.
        regulatory_instrument_id: The instrument whose Roles form the candidate pool.
        single_tenant_graph: Read handle on the single-tenant graph.
        embed_model: Embedding model name.
        thresholds: The Role thresholds (review only).
        call_embedding: Embedding caller, or `None` for the default.
        emitter: Log emitter, or `None` for the default.

    Returns:
        The `DedupResult` for the Role nodes.

    Raises:
        LlmProviderError: An embedding call failed; nothing has been written.
    """
    return dedup.dedupe_canonical_nodes(
        graph.role_nodes,
        kind=ROLE,
        single_tenant_graph=single_tenant_graph,
        model=embed_model,
        thresholds=thresholds,
        instrument_id=regulatory_instrument_id,
        call_embedding=call_embedding,
        emitter=emitter,
    )


def _persist_role_reviews(
    target: GraphWriteTarget,
    single_tenant_graph: GraphHandle,
    near_misses: tuple[NearMissPair, ...],
    *,
    regulatory_instrument_id: str,
    values: ValueSource,
    emitter: LogEmitter | None,
) -> int:
    """Persist a `Role` `PendingReview` for every Role near miss without an unresolved review.

    Args:
        target: The gateway target of the single-tenant graph.
        single_tenant_graph: Read handle used to find the already-reviewed pairs.
        near_misses: The Role near-miss pairs the Role dedup pass cited.
        regulatory_instrument_id: The instrument, for the log entries.
        values: Source of the generated review ids and timestamps.
        emitter: Log emitter, or `None` for the default.

    Returns:
        The number of reviews this call persisted; 0 when every pair already had one.

    Side Effects:
        Submits one gateway group when there is a new review, and emits one log entry per Role
        near miss plus one for the persisted / already-reviewed counts.
    """
    for near_miss in near_misses:
        emit_log_entry(
            component=_COMPONENT,
            action=_DEDUP_ACTION,
            entity_id=near_miss.incoming_id,
            outcome="near_miss",
            extra={
                "kind": ROLE.label,
                "role_name": near_miss.incoming_text,
                "nearest_role_name": near_miss.nearest_existing_text,
                "similarity": near_miss.similarity,
            },
            emitter=emitter,
        )
    new_near_misses = drop_already_reviewed(single_tenant_graph, near_misses, kind=ROLE)
    persist_pending_reviews(target, new_near_misses, kind=ROLE, values=values)
    if near_misses:
        emit_log_entry(
            component=_COMPONENT,
            action=_PERSIST_REVIEWS_ACTION,
            entity_id=regulatory_instrument_id,
            outcome="succeeded",
            extra={
                "kind": ROLE.label,
                "created": len(new_near_misses),
                "already_reviewed": len(near_misses) - len(new_near_misses),
            },
            emitter=emitter,
        )
    return len(new_near_misses)


def merge_baseline_graph(  # noqa: PLR0913 -- the two read handles, the gateway seam and the merge's own options; no natural grouping
    regulatory_instrument_id: str,
    *,
    baseline_graph: GraphHandle,
    single_tenant_graph: GraphHandle,
    submitter: GroupSubmitter,
    single_tenant_graph_name: str,
    baseline_graph_name: str,
    audit_event_id: str,
    embed_model: str,
    merge_threshold: float | None,
    review_threshold: float | None,
    role_review_threshold: float | None,
    call_embedding: EmbeddingCaller | None = None,
    emitter: LogEmitter | None = None,
    governance: GovernanceImport | None = None,
    values: ValueSource = SYSTEM_VALUES,
) -> MergeResult:
    """Merge one regulation's `{short}_baseline` graph into `single_tenant_graph`.

    Add/merge-only: an existing canonical Obligation/Capability node's own properties are never
    overwritten; only an incoming duplicate's edges are rewired onto it. The create-only and
    set-if-null decisions are made in `graph_writer` from a read before each gateway group is
    submitted.

    Args:
        regulatory_instrument_id: Id of the RegulatoryInstrument whose baseline is merged.
        baseline_graph: Read handle on the regulation's `{short}_baseline` graph.
        single_tenant_graph: Read handle on the single-tenant graph; all writes to it go through
            `submitter`.
        submitter: The Graph Write Gateway seam; a write is submitted as a group on the named
            graph under `audit_event_id`.
        single_tenant_graph_name: Name of the single-tenant graph that groups are submitted to.
        baseline_graph_name: Name of the baseline graph that incoming-embedding backfills are
            submitted to.
        audit_event_id: Audit event the run's groups are logged under.
        embed_model: Embedding model name used for the semantic dedup comparison.
        merge_threshold: Cosine similarity at or above which a semantic match is accepted.
            Required: `None` means the configured threshold was never resolved and is an error.
        review_threshold: Cosine similarity at or above which a Capability near miss is raised
            for review; a pair scoring below it raises none. `0.0` hides nothing. Required:
            `None` is an error, and so is a value above `merge_threshold`.
        role_review_threshold: Cosine similarity at or above which a Role near miss (two Roles of
            this instrument) is raised for review; a pair scoring below it raises none. `0.0`
            hides nothing. A Role is never merged here. Required: `None` is an error.
        call_embedding: Embedding caller for the dedup pass, or `None` for the default.
        emitter: Log emitter, or `None` for the default.
        governance: Carries the ingesting caller's `owner` for the Policy/Standard/Control tree of
            an internal-sourced baseline. Defaults to an ownerless `GovernanceImport`, so such a
            baseline fails closed; an external baseline never reads it.
        values: Source of what the merge generates at write time (a created Capability's
            `created_at`). It is drawn while a group is built, so the group carries the value and
            a replay reproduces it.

    Returns:
        A `MergeResult` with the baseline's Obligation ids, the Capability canonical ids, the
        near-miss pairs, the pending-review count (Capability and Role reviews together), the
        Role near-miss and new Role counts and the new/matched/governance counts.

    Raises:
        CompanyMergeConfigurationError: `merge_threshold`, `review_threshold` or
            `role_review_threshold` is `None`, or the review threshold is above the merge
            threshold; raised before either graph receives a call.
        GovernanceOwnerRequiredError: The baseline carries a governance tree and `governance`
            has no owner; raised before any governance write.
        LlmProviderError: An embedding call in the dedup pass failed; nothing has been written.
        CompanyMergePersistenceError: A read failed, the gateway did not take a group, or a
            classification edge endpoint exists nowhere.
        CompanyMergeValidationError: A merge-marker chain has a cycle or ends at a node that
            exists nowhere.

    Side Effects:
        Submits gateway groups to the single-tenant graph (Role/Requirement/Obligation/Capability
        nodes, governance drafts, PracticeArea/RiskPath nodes, rewired edges, embedding
        backfills, Capability and Role `PendingReview` nodes) and to the baseline graph (incoming
        embedding backfills). The graph's legacy edges are stamped before its first group. Emits log
        entries for the run and for each dedup decision.
    """
    thresholds = capability_thresholds(merge_threshold, review_threshold)
    role_review_thresholds = role_thresholds(role_review_threshold)

    targets = _MergeTargets(
        single_tenant=GraphWriteTarget(submitter, single_tenant_graph_name, audit_event_id),
        baseline=GraphWriteTarget(submitter, baseline_graph_name, audit_event_id),
    )
    graph = graph_reader.read_baseline_graph(baseline_graph, regulatory_instrument_id)
    governance = governance or GovernanceImport()

    # A node a Compliance Officer cleanup merge absorbed is dropped from the baseline before any
    # dedup call or write and mapped to its terminal survivor, so the rewiring below lands its
    # edges there instead of minting the duplicate again.
    resolution = redirects.resolve_redirects(single_tenant_graph, graph, emitter=emitter)
    graph = resolution.baseline

    capability_dedup = dedup.dedupe_canonical_nodes(
        graph.capability_nodes,
        kind=CAPABILITY,
        single_tenant_graph=single_tenant_graph,
        model=embed_model,
        thresholds=thresholds,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    # Review-only: cites near Role pairs and never rewrites a Role id. It runs here, before any
    # write, so an embedding failure leaves the graph untouched.
    role_dedup = _dedupe_roles(
        graph,
        regulatory_instrument_id=regulatory_instrument_id,
        single_tenant_graph=single_tenant_graph,
        embed_model=embed_model,
        thresholds=role_review_thresholds,
        call_embedding=call_embedding,
        emitter=emitter,
    )
    new_role_count = sum(1 for r in role_dedup.resolutions if r.match_kind == "new")

    # Read before any write so the count is the delta the passthrough below makes (ids a cleanup
    # merge absorbed were dropped just above).
    new_obligation_count = graph_writer.count_new_obligations(
        single_tenant_graph, tuple(node.id for node in graph.obligation_nodes)
    )

    # Only now, having completed the dedup pass with no exception, is
    # anything written -- "abort with no partial write" on a raised
    # LlmProviderError is therefore automatic, not enforced by a try/except.
    graph_writer.persist_role_and_requirement_passthrough(
        targets.single_tenant,
        single_tenant_graph,
        regulatory_instrument_id,
        graph.regulatory_instrument_properties,
        graph.role_nodes,
        graph.requirement_nodes,
        graph.provenance_edges,
    )
    graph_writer.persist_obligation_passthrough(
        targets.single_tenant, single_tenant_graph, graph.obligation_nodes
    )
    graph_writer.persist_canonical_nodes(
        targets.single_tenant,
        single_tenant_graph,
        graph.capability_nodes,
        capability_dedup.resolutions,
        kind=CAPABILITY,
        values=values,
    )

    # persist_rewired_edges only rewrites a Capability or Policy target, and Standard/Control are
    # passthrough nodes, so the mapping carries Capability, redirected-node and (below) Policy
    # entries alone.
    canonical_id_by_incoming_id: dict[str, str] = {
        resolution.incoming_id: resolution.canonical_id
        for resolution in capability_dedup.resolutions
    }
    canonical_id_by_incoming_id.update(
        redirects.compose_with_resolutions(resolution.mapping, capability_dedup.resolutions)
    )

    # The draft-governance import (shared with restore) is a no-op for an external-sourced
    # baseline; otherwise Policy/Standard/Control are written `draft`, owned by the caller,
    # create-only and never converged.
    governance_counts = governance_count_fields(
        governance_import.import_draft_governance(
            graph,
            single_tenant_graph=single_tenant_graph,
            governance=governance,
            canonical_id_by_incoming_id=canonical_id_by_incoming_id,
            write_drafts=partial(
                graph_writer.persist_governance_drafts, targets.single_tenant, values=values
            ),
        )
    )

    # Runs after Capability dedup and the governance import and before the edge rewiring, so the
    # classification endpoints exist and the mapping is complete when the edges are validated.
    _persist_classification_passthrough(
        targets.single_tenant, single_tenant_graph, graph, canonical_id_by_incoming_id
    )

    # One rewiring call covers the regulatory-spine, governance and classification edges (the
    # latter two are empty for an external baseline). The last group written to the single-tenant
    # graph asks for the digest checkpoint: the embedding backfill when it has something to
    # write, otherwise the rewired edges.
    backfill_follows = bool(capability_dedup.embedding_backfills)
    graph_writer.persist_rewired_edges(
        targets.single_tenant,
        single_tenant_graph,
        graph.bare_edges + graph.governance_edges + graph.classification_edges,
        canonical_id_by_incoming_id,
        checkpoint_requested=not backfill_follows,
    )

    graph_writer.backfill_canonical_embeddings(
        targets.single_tenant,
        single_tenant_graph,
        kind=CAPABILITY,
        embeddings=capability_dedup.embedding_backfills,
        checkpoint_requested=backfill_follows,
    )
    # Cache a freshly computed incoming Capability embedding on its own {short}_baseline node, so a
    # later merge over the same unchanged baseline reuses it instead of paying another embedding
    # call.
    graph_writer.backfill_incoming_capability_embeddings(
        targets.baseline,
        baseline_graph,
        embeddings=capability_dedup.incoming_embedding_backfills,
    )

    # A redirected Capability is a matched one: it resolved onto its (composed) survivor.
    redirected_capability_ids = tuple(
        redirects.compose_with_resolutions(
            resolution.by_kind["Capability"], capability_dedup.resolutions
        ).values()
    )
    new_capability_count = sum(1 for r in capability_dedup.resolutions if r.match_kind == "new")
    matched_capability_count = (
        len(capability_dedup.resolutions) - new_capability_count + len(redirected_capability_ids)
    )
    emit_log_entry(
        component=_COMPONENT,
        action=_MERGE_ACTION,
        entity_id=regulatory_instrument_id,
        outcome="succeeded",
        extra={
            **graph_writer.classification_write_counts(graph),
            "new_obligations": new_obligation_count,
            "new_capabilities": new_capability_count,
            "matched_capabilities": matched_capability_count,
            "new_roles": new_role_count,
            **governance_counts,
        },
        emitter=emitter,
    )

    _log_dedup_decisions(capability_dedup, emitter=emitter)
    # Persist a PendingReview node for every Capability near-miss that has no unresolved review
    # yet, at the point its decision is logged, so a near-miss is never logged without either
    # being persisted or already having a review.
    new_near_misses = drop_already_reviewed(
        single_tenant_graph, capability_dedup.near_misses, kind=CAPABILITY
    )
    persist_pending_reviews(targets.single_tenant, new_near_misses, kind=CAPABILITY, values=values)
    emit_log_entry(
        component=_COMPONENT,
        action=_PERSIST_REVIEWS_ACTION,
        entity_id=regulatory_instrument_id,
        outcome="succeeded",
        extra={
            "created": len(new_near_misses),
            "already_reviewed": len(capability_dedup.near_misses) - len(new_near_misses),
        },
        emitter=emitter,
    )

    new_role_review_count = _persist_role_reviews(
        targets.single_tenant,
        single_tenant_graph,
        role_dedup.near_misses,
        regulatory_instrument_id=regulatory_instrument_id,
        values=values,
        emitter=emitter,
    )

    return MergeResult(
        regulatory_instrument_id=regulatory_instrument_id,
        obligation_ids=tuple(node.id for node in graph.obligation_nodes),
        capability_canonical_ids=(
            *(r.canonical_id for r in capability_dedup.resolutions),
            *redirected_capability_ids,
        ),
        near_misses=capability_dedup.near_misses,
        pending_review_count=len(new_near_misses) + new_role_review_count,
        role_near_miss_count=new_role_review_count,
        new_role_count=new_role_count,
        new_capability_count=new_capability_count,
        matched_capability_count=matched_capability_count,
        new_obligation_count=new_obligation_count,
        **governance_counts,
    )
