"""Role embeddings travel with a curated artifact, so a restore finds Role near misses.

The test drives the export steps that produce an artifact (the embedding backfill on the
baseline graph, then the serialisation) and restores the produced bytes into an empty graph. No
vector is handed to the restore: whatever it scores was written by the export.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from export._gateway_fakes import ExportRig
from graph_gateway._fakes import GatewayRig
from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.export.embeddings import backfill_capability_embeddings
from ps_service.export.serialize import serialize_graph
from ps_service.llm_interface.errors import LlmProviderError
from ps_service.logging import configure
from restore._fixtures import build_restore_artifact, small_graphs
from restore._near_miss_fixtures import POLICY_SYSTEM

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.restore.models import RestoreArtifact
    from restore._gateway_fakes import RestoreRig

INSTRUMENT_ID = "MSA-1.0"
NEAR_ROLE = "Market surveillance authority"
NEAR_ROLE_PLURAL = "Market surveillance authorities"
OTHER_ROLE = "Notified body"
VECTORS: dict[str, list[float]] = {
    NEAR_ROLE: [1.0, 0.0, 0.0],
    NEAR_ROLE_PLURAL: [0.99, 0.1, 0.0],
    OTHER_ROLE: [0.0, 0.0, 1.0],
}


class _NameEmbedding:
    """An `EmbeddingCaller` answering each Role name with its scripted vector."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        self.calls.append(inputs[0])
        vector = VECTORS.get(inputs[0], [0.3, 0.3, 0.3])
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


@pytest.fixture
def export_rig() -> Iterator[ExportRig]:
    """The baseline graph of the instrument, seeded with three Roles it defines."""
    configure()  # the backfill logs through the process default emitter
    gateway_rig = GatewayRig()
    rig = ExportRig(gateway_rig, short_name="MSA")
    _, baseline = small_graphs(instrument_id=INSTRUMENT_ID)
    for node in baseline.nodes:
        properties = dict(node.properties)
        ExportRig.seed_node(rig.baseline, node.label, str(properties["id"]), properties)
    for edge in baseline.edges:
        ExportRig.seed_edge(
            rig.baseline,
            edge.relationship_type,
            (edge.source_label, edge.source_id),
            (edge.target_label, edge.target_id),
            dict(edge.properties),
        )
    for index, name in enumerate(VECTORS):
        role_id = f"msa_role_{index}"
        ExportRig.seed_node(rig.baseline, "Role", role_id, {"name": name, "confidence": 0.9})
        ExportRig.seed_edge(
            rig.baseline,
            "DEFINES",
            ("RegulatoryInstrument", INSTRUMENT_ID),
            ("Role", role_id),
            {"source_ref": "Art. 1"},
        )
    yield rig
    gateway_rig.close()


class _FailingEmbedding:
    """An `EmbeddingCaller` whose provider is down."""

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        del model, inputs, timeout
        message = "provider down"
        raise LlmProviderError(message)


def _exported_artifact(
    rig: ExportRig, caller: _NameEmbedding | None = None, *, embed: bool = True
) -> RestoreArtifact:
    if embed:
        backfill_capability_embeddings(
            rig.baseline_reader(),  # pyright: ignore[reportArgumentType]
            source_type="external",
            model="fake-embed-model",
            call_embedding=caller or _NameEmbedding(),
            **rig.write_target(),  # pyright: ignore[reportArgumentType]
        )
    native, _ = small_graphs(instrument_id=INSTRUMENT_ID)
    return build_restore_artifact(
        instrument_id=INSTRUMENT_ID,
        short_name="MSA",
        native_graph=native,
        baseline_graph=serialize_graph(rig.baseline_reader()),  # pyright: ignore[reportArgumentType]
    )


def test_the_export_embeds_every_role_name(export_rig: ExportRig) -> None:
    caller = _NameEmbedding()

    _exported_artifact(export_rig, caller)

    assert set(VECTORS) <= set(caller.calls)
    assert export_rig.embedding_of("Role", "msa_role_0") == VECTORS[NEAR_ROLE]


def test_a_restore_of_the_exported_artifact_raises_one_role_review(
    export_rig: ExportRig, restore_rig: RestoreRig
) -> None:
    artifact = _exported_artifact(export_rig, _NameEmbedding())

    outcome = restore_rig.restore(artifact, role_review_threshold=0.9)

    reviews = [
        props
        for (label, _), props in restore_rig.graph(POLICY_SYSTEM).nodes.items()
        if label == "PendingReview"
    ]
    assert len(reviews) == 1
    assert reviews[0]["kind"] == "Role"
    assert outcome.near_miss_reviews_created == 1
    names = {reviews[0]["incoming_text"], reviews[0]["nearest_existing_text"]}
    assert names == {NEAR_ROLE, NEAR_ROLE_PLURAL}


def _role_reviews(restore_rig: RestoreRig) -> list[dict[str, object]]:
    return [
        props
        for (label, _), props in restore_rig.graph(POLICY_SYSTEM).nodes.items()
        if label == "PendingReview"
    ]


def test_a_second_restore_of_the_same_artifact_raises_no_second_review(
    export_rig: ExportRig, restore_rig: RestoreRig
) -> None:
    artifact = _exported_artifact(export_rig)
    restore_rig.restore(artifact, role_review_threshold=0.9)

    second = restore_rig.restore(artifact, role_review_threshold=0.9)

    assert len(_role_reviews(restore_rig)) == 1
    assert second.near_miss_reviews_created == 0


def test_a_pair_below_the_role_threshold_raises_no_review(
    export_rig: ExportRig, restore_rig: RestoreRig
) -> None:
    artifact = _exported_artifact(export_rig)

    outcome = restore_rig.restore(artifact, role_review_threshold=0.999)

    assert _role_reviews(restore_rig) == []
    assert outcome.near_miss_reviews_created == 0


def test_an_artifact_without_role_embeddings_restores_with_no_role_review(
    export_rig: ExportRig, restore_rig: RestoreRig
) -> None:
    artifact = _exported_artifact(export_rig, embed=False)

    outcome = restore_rig.restore(artifact, role_review_threshold=0.0)

    assert _role_reviews(restore_rig) == []
    assert outcome.near_miss_reviews_created == 0


def test_a_role_another_instrument_defines_is_never_paired(
    export_rig: ExportRig, restore_rig: RestoreRig
) -> None:
    artifact = _exported_artifact(export_rig)
    live = restore_rig.policy_system
    live.seed_node(("RegulatoryInstrument",), "OTHER-1.0", {"title": "Other"})
    live.seed_node(
        ("Role",),
        "other_role",
        {"name": "Notified bodies", "confidence": 0.9, "embedding": VECTORS[OTHER_ROLE]},
    )
    live.seed_legacy_edge(
        ("DEFINES", "RegulatoryInstrument", "OTHER-1.0", "Role", "other_role"), {}
    )

    restore_rig.restore(artifact, role_review_threshold=0.9)

    reviews = _role_reviews(restore_rig)
    assert len(reviews) == 1
    assert "other_role" not in {reviews[0]["incoming_id"], reviews[0]["nearest_existing_id"]}


def test_an_embedding_failure_at_export_writes_nothing(export_rig: ExportRig) -> None:
    with pytest.raises(LlmProviderError):
        backfill_capability_embeddings(
            export_rig.baseline_reader(),  # pyright: ignore[reportArgumentType]
            source_type="external",
            model="fake-embed-model",
            call_embedding=_FailingEmbedding(),
            **export_rig.write_target(),  # pyright: ignore[reportArgumentType]
        )

    assert export_rig.baseline_groups() == []
    assert export_rig.embedding_of("Role", "msa_role_0") is None
