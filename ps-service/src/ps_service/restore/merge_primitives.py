"""The `policy_system` leg of a restore as gateway primitives.

The offline baseline merge is built from reads of the live graph and an overlay of the leg's
own writes; the only thing a later step reads of an earlier step's writes is node existence by
id (the provenance edges', classification edges' and rewired edges' endpoints):

- every decision that depends on graph content (Capability convergence, merge-marker
  redirects, which ids already exist) is read from `live` and made before anything is built;
- an id counts as existing when `live` holds it or an earlier primitive of this same leg creates
  it (`_Overlay`);
- nothing is written: the result is a tuple of primitives for one `GraphLeg` that the caller
  submits with `ExpectedPosition(P)`, where P is the log position at which `live` was read. The
  caller makes the graph caught up and reads that position first.

The builders are the pure builders of `company_merge.graph_writer`, so a restore and a live
merge decide create-only, set-if-null and edge rewiring identically.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ps_service.company_merge import graph_reader
from ps_service.company_merge.capability_config import capability_thresholds
from ps_service.company_merge.dedup import resolve_capability_convergence_offline
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.generated_values import SYSTEM_VALUES
from ps_service.company_merge.governance_import import (
    governance_count_fields,
    import_draft_governance,
)
from ps_service.company_merge.graph_submission import read_rows
from ps_service.company_merge.graph_writer import (
    ANY_LABEL_EXISTENCE_READ,
    build_canonical_node_primitives,
    build_draft_governance_primitives,
    build_obligation_primitives,
    build_practice_area_and_risk_path_primitives,
    build_rewired_edge_primitives,
    build_role_and_requirement_primitives,
    classification_write_counts,
    missing_classification_endpoints,
    read_existing_ids,
    rewired_edge_endpoint_ids,
    unresolved_classification_endpoint_ids,
)
from ps_service.company_merge.models import GovernanceDraftCounts
from ps_service.company_merge.pending_review import (
    build_pending_review_primitives,
    drop_already_reviewed,
)
from ps_service.company_merge.redirects import compose_with_resolutions, resolve_redirects
from ps_service.company_merge.reviewable_kinds import CAPABILITY
from ps_service.graph_gateway.models import UpsertNode
from ps_service.logging.facade import emit_log_entry
from ps_service.restore.baseline_reader import SerializedGraphReader
from ps_service.restore.errors import ArtifactContentRejectedError, RestoreOwnerRequiredError
from ps_service.restore.leg_primitives import embeddings_by_label

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.generated_values import ValueSource
    from ps_service.company_merge.governance_import import GovernanceImport
    from ps_service.company_merge.models import BaselineGraph, BaselineNode
    from ps_service.export.models import SerializedGraph
    from ps_service.graph_gateway.models import Primitive
    from ps_service.logging.emitter import LogEmitter

__all__ = ["PolicySystemLeg", "build_policy_system_leg"]

_COMPONENT = "restore"
_ACTION = "build_policy_system_leg"
_NODE_LABELS = (
    "Role",
    "Requirement",
    "Obligation",
    "Capability",
    "Policy",
    "Standard",
    "Control",
    "PracticeArea",
    "RiskPath",
)


@dataclass(frozen=True)
class PolicySystemLeg:
    """What one restore does to `policy_system`: its primitives and its audit counts.

    Attributes:
        primitives: The writes, in dependency order; empty when the artifact adds nothing to
            `policy_system`.
        counts: The six classification keys, the four governance keys and
            `near_miss_reviews_created` of the `succeeded` audit entry, each a non-negative
            count.
    """

    primitives: tuple[Primitive, ...]
    counts: dict[str, int]


class _Overlay:
    """The ids `live` held at the read position, plus the ids of the primitives built so far."""

    def __init__(self, existing: dict[str, set[str]]) -> None:
        """Start from `existing`: the ids found in `live`, by label."""
        self.existing = existing
        self._created: dict[str, set[str]] = defaultdict(set)
        self.primitives: list[Primitive] = []

    def add(self, primitives: tuple[Primitive, ...]) -> None:
        """Append `primitives`, counting each `UpsertNode` as an existing id from now on."""
        for primitive in primitives:
            if isinstance(primitive, UpsertNode):
                self._created[primitive.label].add(primitive.id)
        self.primitives.extend(primitives)

    def known(self, label: str) -> set[str]:
        """Ids of `label` that exist in `live` or are created by the primitives added so far."""
        return self.existing.get(label, set()) | self._created.get(label, set())

    def created_ids(self) -> set[str]:
        """Ids of every node the primitives added so far create, across labels."""
        return {node_id for ids in self._created.values() for node_id in ids}


def _check_instrument(
    baseline_graph: SerializedGraph, read: BaselineGraph, instrument_id: str
) -> None:
    """Refuse a manifest id that names no instrument of an artifact that holds some.

    A baseline with no RegulatoryInstrument node at all is a legitimate no-op leg. One that has
    an instrument under another id would merge with every instrument property blanked out.
    """
    if read.regulatory_instrument_properties:
        return
    if any(node.label == "RegulatoryInstrument" for node in baseline_graph.nodes):
        raise ArtifactContentRejectedError(
            f"no RegulatoryInstrument node with id {instrument_id!r} found in the baseline "
            "graph -- the manifest's instrument_id must match the source graph's actual "
            "RegulatoryInstrument.id"
        )


def _candidate_ids(
    read: BaselineGraph,
    new_capability_ids: set[str],
    endpoint_ids: dict[str, set[str]],
) -> dict[str, set[str]]:
    """Per label, every id whose existence the build needs: nodes it may create, edge ends."""
    nodes: dict[str, tuple[BaselineNode, ...]] = {
        "Role": read.role_nodes,
        "Requirement": read.requirement_nodes,
        "Obligation": read.obligation_nodes,
        "Policy": read.policy_nodes,
        "Standard": read.standard_nodes,
        "Control": read.control_nodes,
        "PracticeArea": read.practice_area_nodes,
        "RiskPath": read.risk_path_nodes,
    }
    candidates: dict[str, set[str]] = {
        label: {n.id for n in found} for label, found in nodes.items()
    }
    candidates["Capability"] = set(new_capability_ids)
    # A `DEFINES` edge may name a Role that only the live graph holds (a redirect's terminal).
    candidates["Role"] |= {
        e.target_id for e in read.provenance_edges if e.relationship_type == "DEFINES"
    }
    for label, ids in endpoint_ids.items():
        candidates.setdefault(label, set()).update(ids)
    return candidates


def _read_known_ids_of_unresolved_endpoints(live: GraphHandle, unresolved: set[str]) -> set[str]:
    """The subset of `unresolved` ids that exist in `live` under any label."""
    if not unresolved:
        return set()
    rows = read_rows(live, ANY_LABEL_EXISTENCE_READ, {"ids": sorted(unresolved)})
    return {cast("str", row[0]) for row in rows}


def build_policy_system_leg(
    baseline_graph: SerializedGraph,
    *,
    instrument_id: str,
    live: GraphHandle,
    governance: GovernanceImport,
    merge_threshold: float | None,
    review_threshold: float | None,
    emitter: LogEmitter | None,
    values: ValueSource = SYSTEM_VALUES,
) -> PolicySystemLeg:
    """Build `baseline_graph`'s effect on `policy_system` as primitives, reading `live` only.

    The caller reads `live` at a known log position P (graph caught up, position read, then this
    call) and submits `primitives` as one leg with `ExpectedPosition(P)`; any write in between
    moves the position and the gateway refuses the leg. Primitives come in dependency order:
    instrument, Roles, Requirements, provenance edges, Obligations, Capability mints, draft
    governance, PracticeArea and RiskPath nodes, then the rewired edges (regulatory spine,
    governance and classification), then one `PendingReview` per Capability near miss.

    Business Rules: Capabilities converge onto an existing one by embedding similarity; a draft
    Policy never converges; nodes a cleanup merge absorbed are redirected to their
    survivor; governance content is created only as a `draft` owned by `governance.owner`; every
    Capability near miss scoring at or above the review threshold is recorded as a `PendingReview`
    in the same leg (a pair below it records none), so a failed restore leaves none.

    Args:
        baseline_graph: The parsed baseline leg of the artifact.
        instrument_id: Id of the artifact's `RegulatoryInstrument` (the manifest's
            `instrument_id`); must match one in `baseline_graph` when it holds any.
        live: Handle on the live `policy_system` graph; read only.
        governance: The restoring caller's `owner` and the artifact's Policy embeddings; an
            artifact with a Policy and no owner is refused before `live` is read.
        merge_threshold: Minimum Capability embedding similarity for convergence. Required:
            `None` is an error.
        review_threshold: Minimum similarity at which a Capability near miss is recorded as a
            `PendingReview`; a pair scoring below it records none. `0.0` hides nothing. Required:
            `None` is an error, and so is a value above `merge_threshold`.
        emitter: Log emitter for the leg's log entries; `None` uses the default emitter.
        values: Source of generated values (timestamps); defaults to the system clock.

    Returns:
        The leg's primitives (empty for a baseline that adds nothing) and its audit counts.

    Side Effects:
        Reads `live` (including the unresolved reviews of the near-miss pairs) and emits log
        entries (dedup, redirect and `build_policy_system_leg` success); writes no graph.

    Raises:
        CompanyMergeConfigurationError: `merge_threshold` or `review_threshold` is `None`, or the
            review threshold is above the merge threshold; raised before either graph is read.
        RestoreOwnerRequiredError: the baseline carries governance content and `governance.owner`
            is `None`.
        ArtifactContentRejectedError: `instrument_id` matches no instrument of an artifact that
            holds some.
        CompanyMergeValidationError: a merge-marker chain has a cycle or ends at a node that
            exists nowhere.
        CompanyMergePersistenceError: a node carries a property the gateway cannot store, a
            classification edge endpoint exists nowhere, an edge endpoint has no mapping entry,
            or a read of `live` failed.
    """
    thresholds = capability_thresholds(merge_threshold, review_threshold)
    read = graph_reader.read_baseline_graph(SerializedGraphReader(baseline_graph), instrument_id)
    if governance.owner is None and (
        read.policy_nodes or read.standard_nodes or read.control_nodes
    ):
        raise RestoreOwnerRequiredError
    _check_instrument(baseline_graph, read, instrument_id)

    # Drop nodes a cleanup merge absorbed before any build, and map them to their terminal
    # survivor so the rewiring below re-targets their edges.
    resolution = resolve_redirects(live, read, emitter=emitter)
    read = resolution.baseline
    dedup_result = resolve_capability_convergence_offline(
        read.capability_nodes,
        incoming_embeddings=embeddings_by_label(baseline_graph, "Capability"),
        single_tenant_graph=live,
        kind=CAPABILITY,
        thresholds=thresholds,
        emitter=emitter,
    )
    canonical_id_by_incoming_id = {r.incoming_id: r.canonical_id for r in dedup_result.resolutions}
    canonical_id_by_incoming_id.update(
        compose_with_resolutions(resolution.mapping, dedup_result.resolutions)
    )
    for policy in read.policy_nodes:
        canonical_id_by_incoming_id[policy.id] = policy.id  # a draft never converges

    edges = read.bare_edges + read.governance_edges + read.classification_edges
    endpoint_ids = rewired_edge_endpoint_ids(edges, canonical_id_by_incoming_id)
    candidates = _candidate_ids(
        read,
        {r.canonical_id for r in dedup_result.resolutions if r.match_kind == "new"},
        endpoint_ids,
    )
    overlay = _Overlay(
        {label: read_existing_ids(live, label, ids) for label, ids in candidates.items()}
    )
    existing = overlay.existing
    # Read with the other `live` reads, at the same position, before anything is built.
    # A pair that `live` already holds an unresolved review for is not recorded a second time;
    # ingest drops them with the same helper.
    new_near_misses = drop_already_reviewed(live, dedup_result.near_misses, kind=CAPABILITY)

    overlay.add(
        build_role_and_requirement_primitives(
            existing.get("Role", set()),
            existing.get("Requirement", set()),
            read.regulatory_instrument_id,
            read.regulatory_instrument_properties,
            read.role_nodes,
            read.requirement_nodes,
            read.provenance_edges,
        )
    )
    overlay.add(
        build_obligation_primitives(existing.get("Obligation", set()), read.obligation_nodes)
    )
    overlay.add(
        build_canonical_node_primitives(
            existing.get("Capability", set()),
            read.capability_nodes,
            dedup_result.resolutions,
            kind=CAPABILITY,
            values=values,
        )
    )
    governance_counts = _add_draft_governance(
        overlay, read, live, governance, canonical_id_by_incoming_id, values
    )
    overlay.add(
        build_practice_area_and_risk_path_primitives(
            existing.get("PracticeArea", set()),
            existing.get("RiskPath", set()),
            read.practice_area_nodes,
            read.risk_path_nodes,
        )
    )
    _check_classification_endpoints(overlay, live, read, canonical_id_by_incoming_id)
    overlay.add(
        build_rewired_edge_primitives(
            {label: overlay.known(label) for label in endpoint_ids},
            edges,
            canonical_id_by_incoming_id,
        )
    )

    # Last, so the reviews follow the content they refer to inside the same all-or-nothing leg.
    overlay.add(build_pending_review_primitives(new_near_misses, kind=CAPABILITY, values=values))
    counts = {
        **classification_write_counts(read),
        **governance_count_fields(governance_counts),
        "near_miss_reviews_created": len(new_near_misses),
    }
    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        entity_id=instrument_id,
        outcome="success",
        extra={"primitive_count": len(overlay.primitives)},
        emitter=emitter,
    )
    return PolicySystemLeg(primitives=tuple(overlay.primitives), counts=counts)


def _add_draft_governance(
    overlay: _Overlay,
    read: BaselineGraph,
    live: GraphHandle,
    governance: GovernanceImport,
    canonical_id_by_incoming_id: dict[str, str],
    values: ValueSource,
) -> GovernanceDraftCounts | None:
    """Add the Policy/Standard/Control drafts to `overlay`: create-only, `draft`, caller-owned.

    Returns the draft counts, or `None` when the baseline holds no governance content.
    """
    created_at = values.now().isoformat()

    def collect(
        _graph: GraphHandle,
        policies: tuple[BaselineNode, ...],
        standards: tuple[BaselineNode, ...],
        controls: tuple[BaselineNode, ...],
        /,
        *,
        owner: tuple[str, str],
        policy_embeddings: Mapping[str, tuple[float, ...]] | None,
    ) -> GovernanceDraftCounts:
        nodes, overridden = build_draft_governance_primitives(
            overlay.existing,
            policies,
            standards,
            controls,
            owner=owner,
            policy_embeddings=policy_embeddings,
            created_at=created_at,
        )
        overlay.add(nodes)
        return GovernanceDraftCounts(len(policies), len(standards), len(controls), overridden)

    return import_draft_governance(
        read,
        single_tenant_graph=live,
        governance=governance,
        canonical_id_by_incoming_id=canonical_id_by_incoming_id,
        write_drafts=collect,
    )


def _check_classification_endpoints(
    overlay: _Overlay,
    live: GraphHandle,
    read: BaselineGraph,
    canonical_id_by_incoming_id: dict[str, str],
) -> None:
    """Raise unless every classification edge endpoint exists, in `live` or in this leg."""
    unresolved = unresolved_classification_endpoint_ids(
        read.classification_edges, canonical_id_by_incoming_id
    )
    known = _read_known_ids_of_unresolved_endpoints(live, unresolved) | overlay.created_ids()
    missing = missing_classification_endpoints(
        known, read.classification_edges, canonical_id_by_incoming_id
    )
    if missing:
        raise CompanyMergePersistenceError(
            f"classification edge endpoint(s) {sorted(missing)!r} not found in "
            "the single-tenant graph"
        )
