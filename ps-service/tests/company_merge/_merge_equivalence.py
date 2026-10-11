"""Run the frozen reference and the gateway path over the same baselines (issue #210, AC-BI-002).

`reference_graph` replays the pre-migration writer (`_legacy_reference`) in the original order --
nodes, rewired edges, embedding backfills, pending reviews -- for each regulation of a scenario;
`production_run` drives the real `merge_baseline_graph` over a `MergeRig`. Both draw generated
values from a fresh `stepped_values()` and embeddings from a fresh `OneHotEmbeddingCaller`, and
both start from the same `seeded` nodes, so their `policy_system` digests are comparable (modulo
edge identity; live digest and live interpreter agreement are not verifiable here).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from company_merge._gateway_fakes import FamilyABaseline, family_a_baseline_graph, stepped_values
from company_merge._legacy_reference import (
    LegacyRecordingGraph,
    write_legacy_canonical_nodes,
    write_legacy_embedding_backfill,
    write_legacy_instrument_roles_requirements,
    write_legacy_obligations,
    write_legacy_pending_reviews,
    write_legacy_practice_areas_and_risk_paths,
    write_legacy_rewired_edges,
)
from company_merge._seed_merge_graphs import (
    EMBED_MODEL,
    SIMILARITY_THRESHOLD,
    BaselineGraphReader,
    OneHotEmbeddingCaller,
    SingleTenantStore,
)
from ps_service.company_merge import dedup, graph_reader
from ps_service.company_merge.merge import merge_baseline_graph

if TYPE_CHECKING:
    from company_merge._gateway_fakes import MergeRig
    from ps_service.logging import LogEmitter

SeededNode = tuple[str, str, dict[str, object]]
"""`(label, id, properties)` of a node already in `policy_system` before the first merge."""


def reference_graph(
    steps: tuple[FamilyABaseline, ...],
    emitter: LogEmitter,
    seeded: tuple[SeededNode, ...] = (),
) -> LegacyRecordingGraph:
    """The frozen writer's graph: per regulation, nodes, then rewired edges, backfills, reviews."""
    reference = LegacyRecordingGraph()
    for label, node_id, properties in seeded:
        reference.nodes[(label, node_id)] = dict(properties)
    reads = SingleTenantStore(nodes=reference.nodes)
    values, embeddings = stepped_values(), OneHotEmbeddingCaller()
    for step in steps:
        read = graph_reader.read_baseline_graph(
            BaselineGraphReader(family_a_baseline_graph(step)), step.instrument_id
        )
        outcome = dedup.dedupe_canonical_nodes(
            read.capability_nodes,
            kind="Capability",
            single_tenant_graph=reads,
            model=EMBED_MODEL,
            threshold=SIMILARITY_THRESHOLD,
            call_embedding=embeddings,
            emitter=emitter,
        )
        write_legacy_instrument_roles_requirements(
            reference,
            step.instrument_id,
            read.regulatory_instrument_properties,
            read.role_nodes,
            read.requirement_nodes,
            read.provenance_edges,
        )
        write_legacy_obligations(reference, read.obligation_nodes)
        write_legacy_canonical_nodes(
            reference, read.capability_nodes, outcome.resolutions, kind="Capability", values=values
        )
        write_legacy_practice_areas_and_risk_paths(
            reference, read.practice_area_nodes, read.risk_path_nodes
        )
        write_legacy_rewired_edges(
            reference,
            read.bare_edges + read.governance_edges + read.classification_edges,
            {r.incoming_id: r.canonical_id for r in outcome.resolutions},
        )
        write_legacy_embedding_backfill(reference, outcome.embedding_backfills, kind="Capability")
        write_legacy_pending_reviews(
            reference, outcome.near_misses, kind="Capability", values=values
        )
    return reference


def production_run(
    steps: tuple[FamilyABaseline, ...],
    rig: MergeRig,
    emitter: LogEmitter,
    seeded: tuple[SeededNode, ...] = (),
    **overrides: object,
) -> None:
    """The real `merge_baseline_graph` for each regulation, through the rig's gateway."""
    for label, node_id, properties in seeded:
        rig.policy_system.seed_node((label,), node_id, properties)
    values, embeddings = stepped_values(), OneHotEmbeddingCaller()
    for step in steps:
        keywords: dict[str, object] = {**rig.merge_keywords(), **overrides}
        merge_baseline_graph(
            step.instrument_id,
            baseline_graph=BaselineGraphReader(family_a_baseline_graph(step)),
            single_tenant_graph=rig.legacy,
            embed_model=EMBED_MODEL,
            similarity_threshold=SIMILARITY_THRESHOLD,
            call_embedding=embeddings,
            emitter=emitter,
            values=values,
            **keywords,  # pyright: ignore[reportArgumentType] -- the gateway keywords, overridable
        )
