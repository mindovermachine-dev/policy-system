"""Core types of `ps_service.company_merge`.

The shapes `graph_reader`, `dedup`, `graph_writer` and `merge` build and consume internally, plus
`MergeBaselineGraph`'s own return value. All are plain frozen dataclasses: internal pipeline
plumbing, not PS Conceptual Model types crossing an LLM boundary (nothing here is LLM-structured
output, unlike the Pydantic types of `ps_service.domain_mapper.models`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class BaselineNode:
    """A Role, Requirement, Obligation, Capability, Policy, Standard or Control node of a baseline.

    Read from a `{short}_baseline` graph exactly as the Domain Mapper wrote it. `properties` is used
    for nodes that never carry an embedding.

    Attributes:
        id: The node's baseline-local id.
        properties: The node's properties, string or float valued.
        embedding: The Capability node's cached `embedding` property in `{short}_baseline`, or None
            when it has none. Only the Capability reader populates it; every other node kind leaves
            it None. `dedup.find_best_semantic_match` reuses it in place of a fresh
            `route_embedding` call when present.
    """

    id: str
    properties: dict[str, str | float]
    embedding: tuple[float, ...] | None = None


@dataclass(frozen=True, slots=True)
class ProvenanceEdge:
    """A `RegulatoryInstrument -[:DEFINES|EXPRESSES {source_ref}]-> Role|Requirement` edge.

    Attributes:
        relationship_type: `DEFINES` (target is a Role) or `EXPRESSES` (target is a Requirement).
        target_id: Baseline-local id of the Role or Requirement the instrument points at.
        source_ref: Citation of the regulation text the edge derives from.
    """

    relationship_type: Literal["DEFINES", "EXPRESSES"]
    target_id: str
    source_ref: str


@dataclass(frozen=True, slots=True)
class BareEdge:
    """One edge, as read from the baseline graph, with no properties.

    `HAS`, `SATISFIED_BY`, `REQUIRES`, `GOVERNED_BY`, `SUPPORTED_BY` or `IMPLEMENTED_BY`:
    Role-[:HAS]->Obligation | Requirement-[:SATISFIED_BY]->Obligation |
    Obligation-[:REQUIRES]->Capability | Capability-[:GOVERNED_BY]->Policy |
    Policy-[:SUPPORTED_BY]->Standard | Standard-[:IMPLEMENTED_BY]->Control. `COVERS`, `OWNS`,
    `MITIGATED_BY` or `VERIFIED_BY`: PracticeArea-[:COVERS]->Capability |
    PracticeArea-[:OWNS]->Policy | RiskPath-[:MITIGATED_BY]->Capability |
    RiskPath-[:VERIFIED_BY]->Control. The governance and classification edge types reuse this one
    type rather than a parallel one.

    Endpoint ids are baseline-local. Only a `REQUIRES` edge's Capability target, a `GOVERNED_BY`
    edge's Capability source and Policy target, a `COVERS`/`MITIGATED_BY` edge's Capability target
    and an `OWNS` edge's Policy target are rewritten to their canonical ids before the edge is
    persisted; every other endpoint (Role, Requirement, Obligation, Standard, Control, PracticeArea,
    RiskPath) is a passthrough node whose baseline-local id is already final.

    Attributes:
        relationship_type: One of the ten edge types above.
        source_id: Baseline-local id of the source node.
        target_id: Baseline-local id of the target node.
    """

    relationship_type: Literal[
        "HAS",
        "SATISFIED_BY",
        "REQUIRES",
        "GOVERNED_BY",
        "SUPPORTED_BY",
        "IMPLEMENTED_BY",
        "COVERS",
        "OWNS",
        "MITIGATED_BY",
        "VERIFIED_BY",
    ]
    source_id: str
    target_id: str


@dataclass(frozen=True, slots=True)
class BaselineGraph:
    """The complete contents of one regulation's `{short}_baseline` graph.

    Read back by `graph_reader.read_baseline_graph` and consumed by `MergeBaselineGraph`. The
    governance fields are empty tuples for an external-sourced baseline: Policy, Standard and
    Control are only derived for `source_type: "internal"`. The classification fields are likewise
    empty tuples for a baseline that never went through the internal-seed adapter, which is the only
    author of PracticeArea and RiskPath nodes and of their
    `COVERS`/`OWNS`/`MITIGATED_BY`/`VERIFIED_BY` edges.

    Attributes:
        regulatory_instrument_id: Id of the baseline's RegulatoryInstrument.
        regulatory_instrument_properties: Properties of the instrument node.
        role_nodes: Role nodes.
        requirement_nodes: Requirement nodes.
        obligation_nodes: Obligation nodes.
        capability_nodes: Capability nodes, each carrying its cached embedding when it has one.
        provenance_edges: `DEFINES`/`EXPRESSES` edges from the instrument.
        bare_edges: The `HAS`, `SATISFIED_BY` and `REQUIRES` edges.
        policy_nodes: Policy nodes; empty for an external baseline.
        standard_nodes: Standard nodes; empty for an external baseline.
        control_nodes: Control nodes; empty for an external baseline.
        governance_edges: The `GOVERNED_BY`, `SUPPORTED_BY` and `IMPLEMENTED_BY` edges.
        practice_area_nodes: PracticeArea nodes.
        risk_path_nodes: RiskPath nodes.
        classification_edges: The `COVERS`, `OWNS`, `MITIGATED_BY` and `VERIFIED_BY` edges.
    """

    regulatory_instrument_id: str
    regulatory_instrument_properties: dict[str, object]
    role_nodes: tuple[BaselineNode, ...]
    requirement_nodes: tuple[BaselineNode, ...]
    obligation_nodes: tuple[BaselineNode, ...]
    capability_nodes: tuple[BaselineNode, ...]
    provenance_edges: tuple[ProvenanceEdge, ...]
    bare_edges: tuple[BareEdge, ...]
    policy_nodes: tuple[BaselineNode, ...] = ()
    standard_nodes: tuple[BaselineNode, ...] = ()
    control_nodes: tuple[BaselineNode, ...] = ()
    governance_edges: tuple[BareEdge, ...] = ()
    practice_area_nodes: tuple[BaselineNode, ...] = ()
    risk_path_nodes: tuple[BaselineNode, ...] = ()
    classification_edges: tuple[BareEdge, ...] = ()


@dataclass(frozen=True, slots=True)
class GovernanceDraftCounts:
    """What `graph_writer.persist_governance_drafts` wrote for one restore or internal ingest.

    The counts describe the input tree, not the nodes actually created. A restore never imports an
    authored `approved`/`proposed` status (the customer's own governance decides that), so
    `status_overridden` lets the audit entry show that an override happened.

    Attributes:
        policies: Number of Policy nodes in the tree.
        standards: Number of Standard nodes in the tree.
        controls: Number of Control nodes in the tree.
        status_overridden: Number of nodes whose artifact carried a `status` other than `"draft"`.
    """

    policies: int
    standards: int
    controls: int
    status_overridden: int


@dataclass(frozen=True, slots=True)
class ExistingCanonicalNode:
    """One Capability already present in the single-tenant graph.

    As read by `dedup.read_existing_canonical_index`, or an in-memory stand-in for one just
    minted or updated during the same dedup run. Obligation is not a canonical node: it is passed
    through, not deduped.

    Attributes:
        id: The canonical node's id.
        text: The Capability's `name`, the text compared during dedup.
        embedding: The node's cached embedding, or None when none has been computed or cached.
    """

    id: str
    text: str  # Capability.name
    embedding: tuple[float, ...] | None


@dataclass(frozen=True, slots=True)
class NearMissPair:
    """A near-miss pair, surfaced for review and never merged automatically.

    Either a below-threshold pair, or a pair whose only qualifying match was excluded as a same-run
    mint: the incoming node's best-scoring candidate cleared the threshold, but that candidate was
    minted earlier in the same run (never a pre-existing canonical node), so it was not an eligible
    merge target and the pair is recorded here instead of merged.

    Attributes:
        incoming_id: Id of the incoming node.
        incoming_text: Text of the incoming node.
        nearest_existing_id: Id of its nearest candidate.
        nearest_existing_text: Text of that candidate.
        similarity: Embedding similarity between the two, in the dedup metric's range.
    """

    incoming_id: str
    incoming_text: str
    nearest_existing_id: str
    nearest_existing_text: str
    similarity: float


@dataclass(frozen=True, slots=True)
class CanonicalResolution:
    """The outcome of resolving one incoming Capability node to its canonical id.

    Attributes:
        incoming_id: Baseline-local id of the incoming node.
        canonical_id: Id of the canonical node it resolved to (its own when newly minted).
        match_kind: `exact` (same key as an existing node), `semantic` (embedding match), `new`
            (minted).
        embedding: The node's own embedding, written into its properties at mint time; meaningful
            only when `match_kind` is `new`, and None when no comparison produced one.
    """

    incoming_id: str
    canonical_id: str
    match_kind: Literal["exact", "semantic", "new"]
    embedding: tuple[float, ...] | None


@dataclass(frozen=True, slots=True)
class SemanticMatchResult:
    """`find_best_semantic_match`'s return value when `existing_index` is non-empty.

    Carries two independent "best" pairs computed from one embedding scan at no extra cost.
    `best_existing_id`/`best_similarity` is the overall best-scoring entry across every entry
    scanned, used only for near-miss citation. `best_eligible_id`/`best_eligible_similarity` is the
    best-scoring entry restricted to the caller's `eligible_ids` (identical to the overall best when
    the caller omits `eligible_ids`), used only for the merge decision; both are None when no
    scanned entry belongs to `eligible_ids`, for example when it is an empty frozenset.

    This type only carries values and performs no I/O. The caller (`dedupe_canonical_nodes`) folds
    `newly_computed_existing_embeddings` into its in-memory working index before the next incoming
    node, so the embeddings are reused within the run, and arranges their persistence onto the
    existing graph nodes through `graph_writer.backfill_canonical_embeddings`, so they are reused
    across runs.

    Attributes:
        best_existing_id: Id of the overall best-scoring existing entry.
        best_similarity: Its similarity to the incoming node.
        best_eligible_id: Id of the best-scoring eligible entry, or None.
        best_eligible_similarity: Its similarity, or None.
        incoming_embedding: The incoming node's embedding used for the comparison.
        newly_computed_existing_embeddings: Existing id to the embedding freshly computed during
            this call; an entry that already carried a cached embedding is excluded.
    """

    best_existing_id: str
    best_similarity: float
    best_eligible_id: str | None
    best_eligible_similarity: float | None
    incoming_embedding: tuple[float, ...]
    newly_computed_existing_embeddings: dict[str, tuple[float, ...]]


@dataclass(frozen=True, slots=True)
class DedupResult:
    """`dedupe_canonical_nodes`'s return value for one kind's pass.

    Attributes:
        resolutions: One `CanonicalResolution` per incoming node.
        near_misses: Pairs surfaced for review instead of merged.
        embedding_backfills: Existing id to embedding, for every canonical node that already existed
            in the graph before this run (in `read_existing_canonical_index`'s original result) and
            whose embedding had to be freshly computed during this run. A node minted during this
            run is excluded: its embedding is written as part of its own properties at mint time,
            never by backfill. `merge.py` passes this to
            `graph_writer.backfill_canonical_embeddings` after the dedup pass and all node
            and edge writes complete.
        incoming_embedding_backfills: Incoming id to embedding, for every incoming node whose own
            embedding had to be freshly computed because its `BaselineNode.embedding` was None.
            Only the Capability pass fills it today. `merge.py` passes it to
            `graph_writer.backfill_incoming_capability_embeddings`, which targets the incoming
            `{short}_baseline` graph's Capability nodes, unlike `embedding_backfills`, which targets
            the single-tenant graph's canonical nodes. Defaults to `{}`, as an offline resolution
            that never calls `route_embedding` has none to report.
    """

    resolutions: tuple[CanonicalResolution, ...]
    near_misses: tuple[NearMissPair, ...]
    embedding_backfills: dict[str, tuple[float, ...]]
    incoming_embedding_backfills: dict[str, tuple[float, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PendingReviewRecord:
    """One persisted `PendingReview` node, as read back from FalkorDB.

    Distinct from `NearMissPair`: it adds `id`, `kind` and `created_at`, which the pre-persistence,
    in-memory-only `NearMissPair` has no business carrying.

    Attributes:
        id: The review's unique id.
        kind: Label of the registered reviewable kind the pair belongs to.
        incoming_id: Id of the incoming node of the pair.
        incoming_text: Text of the incoming node.
        nearest_existing_id: Id of its nearest candidate.
        nearest_existing_text: Text of that candidate.
        similarity: Embedding similarity between the two.
        created_at: ISO-8601 instant the review node was created.
    """

    id: str
    kind: str
    incoming_id: str
    incoming_text: str
    nearest_existing_id: str
    nearest_existing_text: str
    similarity: float
    created_at: str


@dataclass(frozen=True, slots=True)
class ResolveOutcome:
    """The result of resolving one `PendingReview`.

    `decision="keep-separate"` leaves `winner_id`/`loser_id` as None: only the `PendingReview`
    record itself is removed. `decision="merge"` populates both with the deterministically chosen
    winner and loser canonical node ids. `decision` is a plain `str`, not a `Literal`, mirroring
    `ResolveReviewResponse`'s wire shape (`api/models.py`).

    Attributes:
        review_id: Id of the resolved review.
        decision: The decision applied, `"keep-separate"` or `"merge"`.
        winner_id: Id of the surviving canonical node for a merge, else None.
        loser_id: Id of the node merged away for a merge, else None.
    """

    review_id: str
    decision: str
    winner_id: str | None = None
    loser_id: str | None = None


@dataclass(frozen=True, slots=True)
class MergeResult:
    """`merge_baseline_graph`'s return value: `MergeBaselineGraph`'s outcome.

    The counts are facts for the audit trail, not approximations.

    Attributes:
        regulatory_instrument_id: Id of the merged instrument.
        obligation_ids: Ids of the Obligations passed through (never deduped). An Obligation a
            cleanup merge absorbed is not listed: nothing is minted for it.
        capability_canonical_ids: Canonical ids the incoming Capabilities resolved to, those a
            cleanup merge absorbed included (each resolves to its survivor), because the run
            summary counts every Capability the baseline matched.
        near_misses: Capability near-miss pairs surfaced for review.
        pending_review_count: Number of `PendingReview` nodes this call persisted, Capability and
            Role together: one per near miss that had no unresolved review yet, so 0 on a re-run
            over unchanged input.
        role_near_miss_count: The Role share of `pending_review_count`: `Role` reviews this call
            persisted. 0 on a re-run, since an already reviewed pair is not raised again.
        new_role_count: Roles of the instrument the Role comparison treated as new (not already
            held under the same id). Roles are never merged, so this is the number minted.
        new_capability_count: Capabilities the dedup pass minted (`match_kind == "new"`).
        matched_capability_count: Capabilities resolved onto an existing node (`exact` or
            `semantic`), plus the Capabilities a cleanup-merge marker redirected.
        new_obligation_count: Obligations this call wrote that did not already exist in the
            single-tenant graph, read before the write; ids dropped by a cleanup-merge redirect are
            never counted.
        governance_policies: Policy nodes the draft-governance import covered; 0 for a baseline with
            no Policy/Standard/Control.
        governance_standards: Standard nodes covered by the import.
        governance_controls: Control nodes covered by the import.
        governance_status_overridden: Nodes whose authored status was forced to `"draft"`; the same
            four counts a restore records.
    """

    regulatory_instrument_id: str
    obligation_ids: tuple[str, ...]
    capability_canonical_ids: tuple[str, ...]
    near_misses: tuple[NearMissPair, ...]
    pending_review_count: int = 0
    role_near_miss_count: int = 0
    new_role_count: int = 0
    new_capability_count: int = 0
    matched_capability_count: int = 0
    new_obligation_count: int = 0
    governance_policies: int = 0
    governance_standards: int = 0
    governance_controls: int = 0
    governance_status_overridden: int = 0
