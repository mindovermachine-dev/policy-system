"""ps_service.company_merge core types — the shapes `graph_reader.py`/
`dedup.py`/`graph_writer.py`/`merge.py` build and consume internally, plus
`MergeBaselineGraph`'s own return value.

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
    """A Role, Requirement, Obligation, or Capability node read back from a
    {short}_baseline graph, exactly as Domain Mapper wrote it. Used for
    Role/Requirement/Obligation, which never carry an embedding -- this
    shape is deliberately NOT reused for the properties dict Company Merge
    itself writes onto a canonical Capability node (see
    CanonicalNodeProperties below, S1's fix)."""

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
    """Role-[:HAS]->Obligation | Requirement-[:SATISFIED_BY]->Obligation |
    Obligation-[:REQUIRES]->Capability, as read from the baseline graph.
    Endpoint ids here are BASELINE-LOCAL -- since #42 only a `REQUIRES`
    edge's Capability target is rewritten to its canonical id before being
    persisted; every other endpoint (Role, Requirement, Obligation) is a
    passthrough node whose baseline-local id is already final (§6)."""

    relationship_type: Literal["HAS", "SATISFIED_BY", "REQUIRES"]
    source_id: str
    target_id: str


@dataclass(frozen=True, slots=True)
class BaselineGraph:
    """The complete contents of one regulation's {short}_baseline graph,
    read back by graph_reader.read_baseline_graph -- MergeBaselineGraph's
    input."""

    regulatory_instrument_id: str
    regulatory_instrument_properties: dict[str, object]
    role_nodes: tuple[BaselineNode, ...]
    requirement_nodes: tuple[BaselineNode, ...]
    obligation_nodes: tuple[BaselineNode, ...]
    capability_nodes: tuple[BaselineNode, ...]
    provenance_edges: tuple[ProvenanceEdge, ...]
    bare_edges: tuple[BareEdge, ...]


@dataclass(frozen=True, slots=True)
class ExistingCanonicalNode:
    """One Capability already present in the single-tenant graph, as read by
    dedup.read_existing_canonical_index, OR an in-memory stand-in for one
    just minted/updated during this same dedup run (§5.4). `embedding` is
    None when this node has never had one computed/cached. (Since #42
    Obligation is not a canonical node -- it is passed through, not deduped.)"""

    id: str
    text: str  # Capability.name
    embedding: tuple[float, ...] | None


@dataclass(frozen=True, slots=True)
class NearMissPair:
    """AC-004: a below-threshold pair, surfaced, never merged."""

    incoming_id: str
    incoming_text: str
    nearest_existing_id: str
    nearest_existing_text: str
    similarity: float


@dataclass(frozen=True, slots=True)
class CanonicalResolution:
    """The outcome of resolving one incoming Capability node to its
    canonical id in the single-tenant graph."""

    incoming_id: str
    canonical_id: str
    match_kind: Literal["exact", "semantic", "new"]
    embedding: tuple[float, ...] | None  # this node's OWN embedding, to be
    # written into its properties at mint time (match_kind == "new" only)


@dataclass(frozen=True, slots=True)
class SemanticMatchResult:
    """find_best_semantic_match's return value when existing_index is
    non-empty (§5.3, B2's fix). `newly_computed_existing_embeddings` holds
    every existing_index entry's embedding that had to be freshly computed
    during THIS call (existing_id -> embedding) -- an entry that already
    carried a cached embedding is excluded, since nothing was computed for
    it. This type only carries values, it performs no I/O: the caller
    (dedupe_canonical_nodes) is responsible for (a) folding these into its
    own in-memory working index before the next incoming node is processed
    -- closing the within-run reuse gap -- and (b) arranging their eventual
    persistence onto the already-existing graph nodes they belong to via
    graph_writer.backfill_canonical_embeddings -- closing the across-run
    reuse gap. See §5.4/§5.5/§6.2 for the full mechanism."""

    best_existing_id: str
    best_similarity: float
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
class MergeResult:
    """merge_baseline_graph()'s return value -- MergeBaselineGraph's outcome."""

    regulatory_instrument_id: str
    obligation_ids: tuple[str, ...]  # passed through per source since #42, not deduped
    capability_canonical_ids: tuple[str, ...]
    near_misses: tuple[NearMissPair, ...]  # AC-004, Capability


# S1's fix: a properties-dict type distinct from BaselineNode.properties,
# wide enough to legally carry an embedding value. Used exclusively for the
# $properties param graph_writer.persist_canonical_nodes builds for a
# match_kind == "new" Capability resolution, and for the $embedding param
# graph_writer.backfill_canonical_embeddings builds. Never used for
# Role/Requirement/RegulatoryInstrument/Obligation properties (those stay
# dict[str, str | float] via BaselineNode -- they never carry an embedding,
# by design, per AC-008 / #42).
CanonicalNodeProperties = dict[str, str | float | list[float]]
