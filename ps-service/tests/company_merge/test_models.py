"""Tests for ps_service.company_merge.models."""

from __future__ import annotations

import dataclasses

import pytest

from ps_service.company_merge.models import (
    BareEdge,
    BaselineGraph,
    BaselineNode,
    CanonicalResolution,
    DedupResult,
    ExistingCanonicalNode,
    MergeResult,
    NearMissPair,
    ProvenanceEdge,
    SemanticMatchResult,
)

# --- BaselineNode -----------------------------------------------------


def test_baseline_node_mutation_raises() -> None:
    node = BaselineNode(id="obl_risk_assessment_a1b2c3", properties={"text": "Assess risk", "confidence": 0.9})
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.id = "changed"  # type: ignore[misc]


def test_baseline_node_constructs_with_valid_fields() -> None:
    node = BaselineNode(id="obl_risk_assessment_a1b2c3", properties={"text": "Assess risk", "confidence": 0.9})
    assert node.id == "obl_risk_assessment_a1b2c3"
    assert node.properties == {"text": "Assess risk", "confidence": 0.9}


# --- ProvenanceEdge -----------------------------------------------------


def test_provenance_edge_mutation_raises() -> None:
    edge = ProvenanceEdge(relationship_type="DEFINES", target_id="role_manufacturer_a1b2c3", source_ref="Art. 13(1)")
    with pytest.raises(dataclasses.FrozenInstanceError):
        edge.source_ref = "changed"  # type: ignore[misc]


def test_provenance_edge_constructs_with_valid_fields() -> None:
    edge = ProvenanceEdge(
        relationship_type="EXPRESSES", target_id="CRA-1.0_req_art_13.1", source_ref="Art. 13(1)"
    )
    assert edge.relationship_type == "EXPRESSES"
    assert edge.target_id == "CRA-1.0_req_art_13.1"


# --- BareEdge -----------------------------------------------------------


def test_bare_edge_mutation_raises() -> None:
    edge = BareEdge(relationship_type="HAS", source_id="role_manufacturer_a1b2c3", target_id="obl_risk_a1b2c3")
    with pytest.raises(dataclasses.FrozenInstanceError):
        edge.target_id = "changed"  # type: ignore[misc]


def test_bare_edge_constructs_with_valid_fields() -> None:
    edge = BareEdge(relationship_type="REQUIRES", source_id="obl_risk_a1b2c3", target_id="cap_logging_a1b2c3")
    assert edge.relationship_type == "REQUIRES"
    assert edge.source_id == "obl_risk_a1b2c3"
    assert edge.target_id == "cap_logging_a1b2c3"


# --- BaselineGraph -------------------------------------------------------


def _baseline_graph() -> BaselineGraph:
    return BaselineGraph(
        regulatory_instrument_id="CRA-1.0",
        regulatory_instrument_properties={"name": "Cyber Resilience Act"},
        role_nodes=(),
        requirement_nodes=(),
        obligation_nodes=(),
        capability_nodes=(),
        provenance_edges=(),
        bare_edges=(),
    )


def test_baseline_graph_mutation_raises() -> None:
    graph = _baseline_graph()
    with pytest.raises(dataclasses.FrozenInstanceError):
        graph.regulatory_instrument_id = "changed"  # type: ignore[misc]


def test_baseline_graph_constructs_with_valid_fields() -> None:
    graph = _baseline_graph()
    assert graph.regulatory_instrument_id == "CRA-1.0"
    assert graph.role_nodes == ()


# --- ExistingCanonicalNode -------------------------------------------------


def test_existing_canonical_node_mutation_raises() -> None:
    node = ExistingCanonicalNode(id="obl_risk_a1b2c3", text="Assess risk", embedding=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.embedding = (0.1, 0.2)  # type: ignore[misc]


def test_existing_canonical_node_constructs_with_valid_fields() -> None:
    node = ExistingCanonicalNode(id="obl_risk_a1b2c3", text="Assess risk", embedding=(0.1, 0.2, 0.3))
    assert node.embedding == (0.1, 0.2, 0.3)


def test_existing_canonical_node_allows_none_embedding_for_uncached_node() -> None:
    node = ExistingCanonicalNode(id="obl_risk_a1b2c3", text="Assess risk", embedding=None)
    assert node.embedding is None


# --- NearMissPair -------------------------------------------------------


def test_near_miss_pair_mutation_raises() -> None:
    pair = NearMissPair(
        incoming_id="obl_new_a1b2c3",
        incoming_text="Report incidents within 24 hours",
        nearest_existing_id="obl_existing_d4e5f6",
        nearest_existing_text="Report incidents promptly",
        similarity=0.62,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        pair.similarity = 0.9  # type: ignore[misc]


def test_near_miss_pair_constructs_with_valid_fields() -> None:
    pair = NearMissPair(
        incoming_id="obl_new_a1b2c3",
        incoming_text="Report incidents within 24 hours",
        nearest_existing_id="obl_existing_d4e5f6",
        nearest_existing_text="Report incidents promptly",
        similarity=0.62,
    )
    assert pair.similarity == 0.62


# --- CanonicalResolution -------------------------------------------------


def test_canonical_resolution_mutation_raises() -> None:
    resolution = CanonicalResolution(
        incoming_id="obl_new_a1b2c3",
        canonical_id="obl_new_a1b2c3",
        match_kind="new",
        embedding=(0.1, 0.2),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolution.match_kind = "exact"  # type: ignore[misc]


def test_canonical_resolution_constructs_with_valid_fields() -> None:
    resolution = CanonicalResolution(
        incoming_id="obl_new_a1b2c3", canonical_id="obl_existing_d4e5f6", match_kind="semantic", embedding=None
    )
    assert resolution.match_kind == "semantic"
    assert resolution.embedding is None


# --- SemanticMatchResult -------------------------------------------------


def test_semantic_match_result_mutation_raises() -> None:
    result = SemanticMatchResult(
        best_existing_id="obl_existing_d4e5f6",
        best_similarity=0.91,
        incoming_embedding=(0.1, 0.2, 0.3),
        newly_computed_existing_embeddings={},
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.best_similarity = 0.5  # type: ignore[misc]


def test_semantic_match_result_constructs_with_valid_fields() -> None:
    result = SemanticMatchResult(
        best_existing_id="obl_existing_d4e5f6",
        best_similarity=0.91,
        incoming_embedding=(0.1, 0.2, 0.3),
        newly_computed_existing_embeddings={},
    )
    assert result.best_existing_id == "obl_existing_d4e5f6"


def test_semantic_match_result_accepts_non_empty_newly_computed_existing_embeddings() -> None:
    """The direct proof (S1/B2) that this field's type shape --
    `dict[str, tuple[float, ...]]` -- actually accepts a real, non-empty
    embedding value, unlike a naive `dict[str, str | float]` that could not
    legally carry a tuple-of-floats value at all."""
    result = SemanticMatchResult(
        best_existing_id="obl_existing_d4e5f6",
        best_similarity=0.91,
        incoming_embedding=(0.1, 0.2, 0.3),
        newly_computed_existing_embeddings={"obl_existing_d4e5f6": (0.11, 0.22, 0.33)},
    )
    assert result.newly_computed_existing_embeddings == {"obl_existing_d4e5f6": (0.11, 0.22, 0.33)}


# --- DedupResult ----------------------------------------------------------


def test_dedup_result_mutation_raises() -> None:
    result = DedupResult(resolutions=(), near_misses=(), embedding_backfills={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.resolutions = ()  # type: ignore[misc]


def test_dedup_result_constructs_with_valid_fields() -> None:
    result = DedupResult(resolutions=(), near_misses=(), embedding_backfills={})
    assert result.embedding_backfills == {}


def test_dedup_result_accepts_non_empty_embedding_backfills() -> None:
    """The direct proof (B2) that `embedding_backfills` -- `dict[str,
    tuple[float, ...]]` -- legitimately carries a real embedding value for
    a pre-existing canonical node whose embedding was freshly computed this
    run."""
    result = DedupResult(
        resolutions=(),
        near_misses=(),
        embedding_backfills={"obl_existing_d4e5f6": (0.11, 0.22, 0.33)},
    )
    assert result.embedding_backfills == {"obl_existing_d4e5f6": (0.11, 0.22, 0.33)}


# --- MergeResult -----------------------------------------------------------


def test_merge_result_mutation_raises() -> None:
    result = MergeResult(
        regulatory_instrument_id="CRA-1.0",
        obligation_ids=(),
        capability_canonical_ids=(),
        near_misses=(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.regulatory_instrument_id = "changed"  # type: ignore[misc]


def test_merge_result_constructs_with_valid_fields() -> None:
    result = MergeResult(
        regulatory_instrument_id="CRA-1.0",
        obligation_ids=("obl_risk_role_x_a1b2c3",),
        capability_canonical_ids=("cap_logging_a1b2c3",),
        near_misses=(),
    )
    assert result.regulatory_instrument_id == "CRA-1.0"
    assert result.obligation_ids == ("obl_risk_role_x_a1b2c3",)
