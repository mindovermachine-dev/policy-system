"""`merge_baseline_graph` -- the `MergeBaselineGraph` public action.

The top-level orchestration (PLAN_REVIEWED.md §7, §10 Increments 13-14) that
wires `graph_reader.read_baseline_graph`, `dedup.dedupe_canonical_nodes`
(Capability), `governance_import.import_draft_governance` and every
`graph_writer` function together.

Flow (§7, with the governance import of issue #227 as item 3a):

0. B1's fix, enforced first, before anything else runs: if
   `similarity_threshold is None`, raise `CompanyMergeConfigurationError` --
   zero graph calls of any kind have been made by this point, not even
   `graph_reader.read_baseline_graph`.
1. Read the regulation's `{short}_baseline` graph.
2. Dedupe Capability nodes against the single-tenant graph. (Obligation is
   Role-scoped since #42 -- a weak entity of exactly one Role, never deduped
   across sources -- so there is no Obligation dedup pass.)
3. Persist RegulatoryInstrument/Role/Requirement/`DEFINES`/`EXPRESSES` as one Graph Write
   Gateway group (#210; a Role/Requirement the graph already holds is left out of the group,
   so it keeps its properties) and Obligation (create-only); persist canonical Capability
   nodes for every `match_kind="new"` resolution (create-only).
3a. (#227) If the baseline carries a Policy/Standard/Control tree (an
    internal-sourced baseline), `governance_import.import_draft_governance`
    writes it as `draft`, owned by `governance.owner`, create-only and never
    converged onto an existing Policy -- the same function restore calls.
    An external-sourced baseline skips this step (a structural no-op); an
    internal one with no owner raises `GovernanceOwnerRequiredError` before
    any governance write.
3b. (#106) Persist PracticeArea/RiskPath nodes as create-only
    passthrough, then validate every `COVERS`/`OWNS`/`MITIGATED_BY`/
    `VERIFIED_BY` edge endpoint exists (AC-BI-008) before any of those four
    edges is written. No dedup, no embedding call for PracticeArea/RiskPath
    (AC-BI-006) -- identity convergence is structural. A baseline with no
    classification-layer content skips this entirely -- a structural no-op
    (AC-BI-009).
4. Persist `HAS`/`SATISFIED_BY`/`REQUIRES`/`GOVERNED_BY`/`SUPPORTED_BY`/
   `IMPLEMENTED_BY`/`COVERS`/`OWNS`/`MITIGATED_BY`/`VERIFIED_BY` edges in one
   `persist_rewired_edges` call, using the combined Capability+Policy
   canonical-id mapping (a `REQUIRES`/`COVERS`/`MITIGATED_BY` edge's
   Capability target is rewritten; a Policy maps onto itself -- see
   `graph_writer.persist_rewired_edges`); then backfill Capability
   embeddings onto the single-tenant graph's canonical nodes, and (issue #31) any
   freshly-computed INCOMING Capability embeddings back onto this
   regulation's own `{short}_baseline` graph
   (`graph_writer.backfill_incoming_capability_embeddings`).
5. Emit one `outcome="succeeded"` entry for the whole call, carrying the
   PracticeArea/RiskPath/classification-edge write counts (AC-BI-011) via
   `extra=graph_writer.classification_write_counts(graph)`. No
   `bind_run_context()` self-bind here -- `run_id` is whatever the caller
   already bound, or `None`.
6. Emit one additional log entry per Capability dedup decision
   (AC-007): one per `CanonicalResolution`, outcome=`match_kind`; one per
   `NearMissPair`, outcome="near_miss".
7. Return `MergeResult`.

If step 2 raises (`LlmProviderError` from an embedding call), the exception
propagates unchanged, with no write yet made, satisfying "abort with no
partial write" as a structural property of call order.

`DedupeCanonicalNodes` is not exposed as its own separately-invocable public
action alongside `merge_baseline_graph`, per the CA doc's exact wording
(PLAN_REVIEWED.md §0.1) -- there is no standalone function a caller invokes
for it; it only ever runs as part of this orchestration.
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
    obligation_redirect,
)
from ps_service.company_merge.errors import CompanyMergeConfigurationError
from ps_service.company_merge.generated_values import SYSTEM_VALUES
from ps_service.company_merge.governance_import import GovernanceImport, governance_count_fields
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.models import MergeResult
from ps_service.company_merge.pending_review import persist_pending_reviews
from ps_service.logging import LogEmitter, emit_log_entry

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.generated_values import ValueSource
    from ps_service.company_merge.models import BaselineGraph, DedupResult
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.llm_interface.client import EmbeddingCaller

__all__ = ["merge_baseline_graph"]

_COMPONENT = "company_merge"
_MERGE_ACTION = "merge_baseline_graph"
_DEDUP_ACTION = "dedupe_canonical_nodes"


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
    """Issue #106: PracticeArea/RiskPath node passthrough + pre-write edge validation.

    No dedup, no embedding call (AC-BI-006) -- identity convergence is
    structural (content-hashed ids minted upstream by `internal_seed`, per
    `docs/artifacts/ps-domain-concepts.md`'s identity notes). A structural
    no-op when the baseline carries no classification-layer content
    (AC-BI-009): `graph_writer.persist_practice_area_and_risk_path_passthrough`
    reads and submits nothing for empty input, and
    `graph_writer.validate_classification_edge_endpoints` returns immediately
    when `classification_edges` is empty (no endpoints to resolve).

    `canonical_id_by_incoming_id` (mutated in place by the Capability pass and the
    draft-governance import above) is passed straight through to
    `validate_classification_edge_endpoints` (AC-BI-008): a `COVERS`/
    `MITIGATED_BY` edge's Capability target or an `OWNS` edge's Policy
    target already has an entry there when it was dedup-resolved; every
    other endpoint (PracticeArea/RiskPath source, `VERIFIED_BY`'s Control
    target) is checked directly against `single_tenant_graph`, since the
    node passthrough call above has already persisted it by this point.
    """
    graph_writer.persist_practice_area_and_risk_path_passthrough(
        target, single_tenant_graph, graph.practice_area_nodes, graph.risk_path_nodes
    )
    graph_writer.validate_classification_edge_endpoints(
        single_tenant_graph, graph.classification_edges, canonical_id_by_incoming_id
    )


def _log_dedup_decisions(dedup_result: DedupResult, *, emitter: LogEmitter | None) -> None:
    """Emit one log entry per `CanonicalResolution` and per `NearMissPair` (AC-007)."""
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


def merge_baseline_graph(  # noqa: PLR0913 -- the two read handles, the gateway seam (#210) and the merge's own options; no natural grouping
    regulatory_instrument_id: str,
    *,
    baseline_graph: GraphHandle,
    single_tenant_graph: GraphHandle,
    submitter: GroupSubmitter,
    single_tenant_graph_name: str,
    baseline_graph_name: str,
    audit_event_id: str,
    embed_model: str,
    similarity_threshold: float | None,
    call_embedding: EmbeddingCaller | None = None,
    emitter: LogEmitter | None = None,
    governance: GovernanceImport | None = None,
    values: ValueSource = SYSTEM_VALUES,
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
    create-only and set-if-null decisions, which are made from a read before the
    gateway group is submitted.

    `governance` (issue #227) carries the ingesting caller's `owner` for the
    Policy/Standard/Control tree of an internal-sourced baseline, which is
    imported as `draft` by `import_draft_governance` (the function restore
    also calls). It defaults to an ownerless `GovernanceImport`, so such a
    baseline fails closed with `GovernanceOwnerRequiredError`; an external
    baseline never reads it.

    `submitter`, `single_tenant_graph_name`, `baseline_graph_name` and `audit_event_id` (issue
    #210) are the Graph Write Gateway seam: a migrated write is submitted as a group on the named
    graph under the run's audit event, while `single_tenant_graph` and `baseline_graph` stay READ
    handles for it. The graph's legacy edges are stamped before its first group, after the
    pre-flight checks and the dedup pass above have completed. `values` is where the merge draws
    what it generates at write time (a created Capability's `created_at`): drawn while a group is
    built, so the group carries it and a replay reproduces it.
    """
    if similarity_threshold is None:
        raise CompanyMergeConfigurationError(
            "similarity_threshold is required -- no default is defined; resolve "
            "PS_COMPANYMERGE_SIMILARITY_THRESHOLD via ServiceConfig before calling "
            "merge_baseline_graph"
        )

    targets = _MergeTargets(
        single_tenant=GraphWriteTarget(submitter, single_tenant_graph_name, audit_event_id),
        baseline=GraphWriteTarget(submitter, baseline_graph_name, audit_event_id),
    )
    graph = graph_reader.read_baseline_graph(baseline_graph, regulatory_instrument_id)
    governance = governance or GovernanceImport()

    capability_dedup = dedup.dedupe_canonical_nodes(
        graph.capability_nodes,
        kind="Capability",
        single_tenant_graph=single_tenant_graph,
        model=embed_model,
        threshold=similarity_threshold,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    # issue #190 (H1): an Obligation a Compliance Officer cleanup merge absorbed is dropped
    # from the baseline here, BEFORE any write, and mapped to its terminal survivor so the
    # rewiring below lands its edges there instead of minting the duplicate again.
    graph, obligation_redirects = obligation_redirect.resolve_obligation_redirects(
        single_tenant_graph, graph, emitter=emitter
    )

    # issue #195: the net-new Obligation count, read BEFORE any write so it is the delta the
    # passthrough below makes (ids a cleanup merge absorbed were dropped just above).
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
        kind="Capability",
        values=values,
    )

    # persist_rewired_edges only ever rewrites a REQUIRES edge's Capability
    # target (and, since issue #54's S4, a GOVERNED_BY edge's Policy
    # target) -- Obligation/Standard/Control are passthrough nodes, so the
    # mapping carries Capability (and, below, Policy) resolutions alone.
    canonical_id_by_incoming_id: dict[str, str] = {
        resolution.incoming_id: resolution.canonical_id
        for resolution in capability_dedup.resolutions
    }
    canonical_id_by_incoming_id.update(obligation_redirects)

    # issue #227 -- the shared draft-governance import (the same function restore calls): a
    # structural no-op for an external-sourced baseline, otherwise Policy/Standard/Control
    # written `draft`, owned by the caller, create-only, never converged.
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

    # issue #106 -- PracticeArea/RiskPath node passthrough, a structural
    # no-op for a baseline with no classification-layer content. Runs after
    # Capability dedup and the governance import complete and before the edge rewiring
    # call below, mirroring the governance import's own placement.
    _persist_classification_passthrough(
        targets.single_tenant, single_tenant_graph, graph, canonical_id_by_incoming_id
    )

    # One rewiring call over the regulatory-spine edges, the governance
    # edges, and the classification edges -- governance_edges/
    # classification_edges are empty for an external baseline, so this is
    # unchanged from before S4/#106 in that case.
    graph_writer.persist_rewired_edges(
        targets.single_tenant,
        single_tenant_graph,
        graph.bare_edges + graph.governance_edges + graph.classification_edges,
        canonical_id_by_incoming_id,
    )

    graph_writer.backfill_canonical_embeddings(
        targets.single_tenant,
        single_tenant_graph,
        kind="Capability",
        embeddings=capability_dedup.embedding_backfills,
    )
    # issue #31 -- the incoming side of the same cost problem: cache a
    # freshly-computed incoming Capability embedding onto its OWN
    # {short}_baseline node, so a later merge run against this same,
    # unchanged baseline (e.g. a re-ingested amended regulation, #19) reuses
    # it instead of paying another route_embedding call.
    graph_writer.backfill_incoming_capability_embeddings(
        targets.baseline,
        baseline_graph,
        embeddings=capability_dedup.incoming_embedding_backfills,
    )

    new_capability_count = sum(1 for r in capability_dedup.resolutions if r.match_kind == "new")
    matched_capability_count = len(capability_dedup.resolutions) - new_capability_count
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
            **governance_counts,
        },
        emitter=emitter,
    )

    _log_dedup_decisions(capability_dedup, emitter=emitter)
    # issue #35, Slice 1 (AC-BI-001/AC-BI-002): persist a PendingReview node
    # for every Capability near-miss, at the same point its own decision is
    # already logged -- never logged without also being persisted.
    persist_pending_reviews(
        targets.single_tenant, capability_dedup.near_misses, kind="Capability", values=values
    )

    return MergeResult(
        regulatory_instrument_id=regulatory_instrument_id,
        obligation_ids=tuple(node.id for node in graph.obligation_nodes),
        capability_canonical_ids=tuple(r.canonical_id for r in capability_dedup.resolutions),
        near_misses=capability_dedup.near_misses,
        pending_review_count=len(capability_dedup.near_misses),
        new_capability_count=new_capability_count,
        matched_capability_count=matched_capability_count,
        new_obligation_count=new_obligation_count,
        **governance_counts,
    )
