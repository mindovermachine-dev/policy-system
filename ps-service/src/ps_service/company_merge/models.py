"""ps_service.company_merge core types.

The shapes `graph_reader.py`/`dedup.py`/`graph_writer.py`/`merge.py` build
and consume internally, plus `MergeBaselineGraph`'s own return value.

Per PLAN_REVIEWED.md §2: all types here are plain frozen dataclasses —
internal pipeline plumbing, not PS Conceptual Model types crossing an LLM
boundary (nothing here is LLM-structured output, unlike
`ps_service.domain_mapper.models`'s Pydantic types).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class BaselineNode:
    """A Role, Requirement, Obligation, or Capability node read from a {short}_baseline graph.

    Exactly as Domain Mapper wrote it. Used for Role/Requirement/Obligation,
    which never carry an embedding -- this shape is deliberately NOT reused
    for the properties dict Company Merge itself writes onto a canonical
    Capability node (see CanonicalNodeProperties below, S1's fix).
    """

    id: str
    properties: dict[str, str | float]


@dataclass(frozen=True, slots=True)
class ProvenanceEdge:
    """RegulatoryInstrument -[:DEFINES|EXPRESSES {source_ref}]-> Role|Requirement."""

    relationship_type: Literal["DEFINES", "EXPRESSES"]
    target_id: str
    source_ref: str


@dataclass(frozen=True, slots=True)
class BareEdge:
    """One edge, as read from the baseline graph.

    `HAS`, `SATISFIED_BY`, `REQUIRES`, `GOVERNED_BY`, `SUPPORTED_BY`, or
    `IMPLEMENTED_BY`: Role-[:HAS]->Obligation | Requirement-[:SATISFIED_BY]->Obligation |
    Obligation-[:REQUIRES]->Capability | Capability-[:GOVERNED_BY]->Policy |
    Policy-[:SUPPORTED_BY]->Standard | Standard-[:IMPLEMENTED_BY]->Control
    (issue #54, S4 -- the governance edge types reuse this same type rather
    than a parallel `GovernanceEdge` type). `COVERS`, `OWNS`, `MITIGATED_BY`,
    or `VERIFIED_BY`: PracticeArea-[:COVERS]->Capability |
    PracticeArea-[:OWNS]->Policy | RiskPath-[:MITIGATED_BY]->Capability |
    RiskPath-[:VERIFIED_BY]->Control (issue #106 -- the classification-layer
    edge types reuse this same type rather than a parallel type). Endpoint
    ids here are BASELINE-LOCAL -- since #42 only a `REQUIRES` edge's
    Capability target (and, since #54, a `GOVERNED_BY` edge's Policy target,
    and, since #106, a `COVERS`/`MITIGATED_BY` edge's Capability target and
    an `OWNS` edge's Policy target) is rewritten to its canonical id before
    being persisted; every other endpoint (Role, Requirement, Obligation,
    Standard, Control, PracticeArea, RiskPath) is a passthrough node whose
    baseline-local id is already final (§6).
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
    """The complete contents of one regulation's {short}_baseline graph.

    Read back by graph_reader.read_baseline_graph -- MergeBaselineGraph's
    input. `policy_nodes`/`standard_nodes`/`control_nodes`/`governance_edges`
    (issue #54, S4) are empty tuples for an external-sourced baseline --
    Policy/Standard/Control are only ever derived for `source_type:
    "internal"` (`DeriveGovernanceArtifacts`, S3). `practice_area_nodes`/
    `risk_path_nodes`/`classification_edges` (issue #106) are likewise empty
    tuples for a baseline that never went through `internal_seed` --
    PracticeArea/RiskPath and their `COVERS`/`OWNS`/`MITIGATED_BY`/
    `VERIFIED_BY` edges are only ever authored by that adapter today.
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
class ExistingCanonicalNode:
    """One Capability already present in the single-tenant graph.

    As read by dedup.read_existing_canonical_index, OR an in-memory stand-in
    for one just minted/updated during this same dedup run (§5.4).
    `embedding` is None when this node has never had one computed/cached.
    (Since #42 Obligation is not a canonical node -- it is passed through,
    not deduped.)
    """

    id: str
    text: str  # Capability.name
    embedding: tuple[float, ...] | None


@dataclass(frozen=True, slots=True)
class NearMissPair:
    """A near-miss pair, surfaced, never merged.

    Either AC-004's original case (a below-threshold pair) or, since issue
    #30 (AC-BI-004), a pair whose only qualifying match was excluded as a
    same-run mint: the incoming node's best-scoring candidate cleared
    threshold, but that candidate was minted earlier in this same run
    (never a pre-existing canonical node), so it was not an eligible merge
    target -- the pair is recorded here instead of merged.
    """

    incoming_id: str
    incoming_text: str
    nearest_existing_id: str
    nearest_existing_text: str
    similarity: float


@dataclass(frozen=True, slots=True)
class CanonicalResolution:
    """The outcome of resolving one incoming Capability node to its canonical id."""

    incoming_id: str
    canonical_id: str
    match_kind: Literal["exact", "semantic", "new"]
    embedding: tuple[float, ...] | None  # this node's OWN embedding, to be
    # written into its properties at mint time (match_kind == "new" only)


@dataclass(frozen=True, slots=True)
class SemanticMatchResult:
    """find_best_semantic_match's return value when existing_index is non-empty.

    §5.3, B2's fix. Since issue #30, carries TWO independent "best" pairs
    computed from one embedding scan, at zero extra cost:
    `best_existing_id`/`best_similarity` is the overall best-scoring entry
    across EVERY entry scanned, used only for near-miss citation;
    `best_eligible_id`/`best_eligible_similarity` is the best-scoring entry
    restricted to the caller's `eligible_ids` (identical to the overall best
    when the caller omits `eligible_ids`), used only for the merge decision.
    `best_eligible_id`/`best_eligible_similarity` are both `None` when no
    scanned entry belongs to `eligible_ids` (e.g. `eligible_ids` is an empty
    frozenset).

    `newly_computed_existing_embeddings` holds every existing_index entry's
    embedding that had to be freshly computed during THIS call (existing_id
    -> embedding) -- an entry that already carried a cached embedding is
    excluded, since nothing was computed for it. This type only carries
    values, it performs no I/O: the caller (dedupe_canonical_nodes) is
    responsible for (a) folding these into its own in-memory working index
    before the next incoming node is processed -- closing the within-run
    reuse gap -- and (b) arranging their eventual persistence onto the
    already-existing graph nodes they belong to via
    graph_writer.backfill_canonical_embeddings -- closing the across-run
    reuse gap. See §5.4/§5.5/§6.2 for the full mechanism.
    """

    best_existing_id: str
    best_similarity: float
    best_eligible_id: str | None
    best_eligible_similarity: float | None
    incoming_embedding: tuple[float, ...]
    newly_computed_existing_embeddings: dict[str, tuple[float, ...]]


@dataclass(frozen=True, slots=True)
class DedupResult:
    """dedupe_canonical_nodes()'s return value for the Capability pass."""

    resolutions: tuple[CanonicalResolution, ...]
    near_misses: tuple[NearMissPair, ...]
    embedding_backfills: dict[str, tuple[float, ...]]
    """existing_id -> embedding, for every PRE-EXISTING canonical node
    (present in read_existing_canonical_index's original result, i.e.
    already persisted before this run started) whose embedding had to be
    freshly computed during this run. A node minted DURING this run is
    deliberately excluded here -- its embedding is written as part of its
    own ON CREATE SET properties at mint time (§6), never via backfill.
    merge.py (§7) passes this straight to
    graph_writer.backfill_canonical_embeddings after both kinds' dedup
    passes and all node/edge writes complete."""


@dataclass(frozen=True, slots=True)
class PendingReviewRecord:
    """One persisted `PendingReview` node, as read back from FalkorDB.

    Issue #35 (near-miss review workflow), §2.3: a distinct type from
    `NearMissPair` above -- adds `id`/`kind`/`created_at`, which
    `NearMissPair` correctly has no business carrying, since it is a
    pre-persistence, in-memory-only shape. Slice 1 only ever constructs the
    params written onto this node (`pending_review.persist_pending_reviews`);
    nothing reads a `PendingReviewRecord` back yet -- that is Slice 2's
    `list_pending_reviews`.
    """

    id: str
    kind: Literal["Capability", "Policy"]
    incoming_id: str
    incoming_text: str
    nearest_existing_id: str
    nearest_existing_text: str
    similarity: float
    created_at: str


@dataclass(frozen=True, slots=True)
class ResolveOutcome:
    """The result of resolving one `PendingReview` (issue #35).

    `decision="keep-separate"` leaves `winner_id`/`loser_id` as `None`
    (AC-BI-004: only the `PendingReview` record itself is removed).
    `decision="merge"` populates both with the deterministically-chosen
    winner/loser canonical node ids (AC-BI-005/006). `decision` is a plain
    `str`, not a `Literal`, mirroring `ResolveReviewResponse`'s own wire
    shape (`api/models.py`) -- the request-side `Literal` is what widens
    slice to slice, not this output type.
    """

    review_id: str
    decision: str
    winner_id: str | None = None
    loser_id: str | None = None


@dataclass(frozen=True, slots=True)
class MergeResult:
    """merge_baseline_graph()'s return value -- MergeBaselineGraph's outcome."""

    regulatory_instrument_id: str
    obligation_ids: tuple[str, ...]  # passed through per source since #42, not deduped
    capability_canonical_ids: tuple[str, ...]
    near_misses: tuple[NearMissPair, ...]  # AC-004, Capability
    # issue #54, S4 -- empty for an external-sourced baseline (no Policy pass ran).
    policy_canonical_ids: tuple[str, ...] = ()
    # issue #35, Slice 5 (AC-BI-010): the run-scoped count of `PendingReview`
    # nodes THIS call actually persisted -- `len(capability near_misses) +
    # len(policy near_misses)`, i.e. exactly the number of
    # `persist_pending_reviews` writes issued during this call (CREATE, not
    # MERGE -- see pending_review.py), never a fresh "all unresolved reviews
    # ever" graph read. Zero by default so every pre-existing `MergeResult(...)`
    # construction in this codebase's tests stays valid unchanged.
    pending_review_count: int = 0


# S1's fix: a properties-dict type distinct from BaselineNode.properties,
# wide enough to legally carry an embedding value. Used exclusively for the
# $properties param graph_writer.persist_canonical_nodes builds for a
# match_kind == "new" Capability resolution, and for the $embedding param
# graph_writer.backfill_canonical_embeddings builds. Never used for
# Role/Requirement/RegulatoryInstrument/Obligation properties (those stay
# dict[str, str | float] via BaselineNode -- they never carry an embedding,
# by design, per AC-008 / #42).
CanonicalNodeProperties = dict[str, str | float | list[float]]
