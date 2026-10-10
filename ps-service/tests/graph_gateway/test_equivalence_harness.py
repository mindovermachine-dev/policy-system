"""The equivalence harness passes for equal write paths (#207 S20, AC-RD-009)."""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from graph_gateway.equivalence_harness import (
    Artifact,
    ArtifactNode,
    EquivalenceResult,
    SyntheticWriter,
    load_artifact,
    parse_artifact,
    run_equivalence,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_CRA_NATIVE = Path(__file__).resolve().parents[3] / "curated-content" / "CRA-1.0" / "native.json"


def _small_artifact() -> Artifact:
    return parse_artifact(
        {
            "nodes": [
                {"label": "ARTICLE", "properties": {"id": "a-1", "order": 1, "text": "one"}},
                {"label": "PARAGRAPH", "properties": {"id": "p-1", "text": "two"}},
                {"label": "PARAGRAPH", "properties": {"id": "p-2", "text": "three"}},
            ],
            "edges": [
                {
                    "source_label": "ARTICLE",
                    "source_id": "a-1",
                    "target_label": "PARAGRAPH",
                    "target_id": target,
                    "relationship_type": "HAS",
                    "properties": dict[str, object](),
                }
                for target in ("p-1", "p-2")
            ],
        }
    )


def test_harness_reports_pass_when_old_and_new_paths_produce_the_same_digest() -> None:
    writer = SyntheticWriter()

    result = run_equivalence(_small_artifact(), writer.old_path, writer.new_path)

    assert result.passed
    assert result.old_digest == result.new_digest
    assert result.old_digest.startswith("sha256:")
    assert (result.node_count, result.edge_count) == (3, 2)


def test_harness_runs_on_the_real_cra_native_artifact() -> None:
    writer = SyntheticWriter()

    result = run_equivalence(load_artifact(_CRA_NATIVE), writer.old_path, writer.new_path)

    assert result.passed
    assert (result.node_count, result.edge_count) == (506, 505)


def _without_last_edge(artifact: Artifact) -> Artifact:
    return Artifact(nodes=artifact.nodes, edges=artifact.edges[:-1])


def _reversed_first_edge(artifact: Artifact) -> Artifact:
    first, *rest = artifact.edges
    flipped = replace(
        first,
        source_label=first.target_label,
        source_id=first.target_id,
        target_label=first.source_label,
        target_id=first.source_id,
    )
    return Artifact(nodes=artifact.nodes, edges=(flipped, *rest))


def _with_ulp_off_embedding(artifact: Artifact) -> Artifact:
    nodes = list(artifact.nodes)
    index = next(i for i, node in enumerate(nodes) if node.embedding is not None)
    values = list(nodes[index].embedding or ())
    values[1] = math.nextafter(values[1], math.inf)
    nodes[index] = replace(nodes[index], embedding=tuple(values))
    return Artifact(nodes=tuple(nodes), edges=artifact.edges)


def _diverging_run(artifact: Artifact, mutate: Callable[[Artifact], Artifact]) -> EquivalenceResult:
    writer = SyntheticWriter()
    return run_equivalence(
        artifact, writer.old_path, lambda original: writer.new_path(mutate(original))
    )


def _artifact_with_embedding() -> Artifact:
    base = _small_artifact()
    secret = ArtifactNode(
        label="PARAGRAPH",
        id="p-3",
        properties={"text": "classified wording"},
        embedding=(0.1, 0.2, 0.3),
    )
    return Artifact(nodes=(*base.nodes, secret), edges=base.edges)


def test_harness_reports_fail_when_the_new_path_drops_an_edge() -> None:
    result = _diverging_run(_small_artifact(), _without_last_edge)

    assert not result.passed
    assert result.old_digest != result.new_digest
    assert (result.differences.edges_only_in_old, result.differences.edges_only_in_new) == (1, 0)
    assert (result.differences.nodes_only_in_old, result.differences.nodes_only_in_new) == (0, 0)


def test_harness_reports_fail_when_the_new_path_alters_one_embedding_value() -> None:
    result = _diverging_run(_artifact_with_embedding(), _with_ulp_off_embedding)

    assert not result.passed
    assert (result.differences.nodes_only_in_old, result.differences.nodes_only_in_new) == (1, 1)
    assert result.differences.edges_only_in_old == result.differences.edges_only_in_new == 0


def test_harness_reports_fail_when_the_new_path_reverses_an_edge() -> None:
    result = _diverging_run(_small_artifact(), _reversed_first_edge)

    assert not result.passed
    assert (result.differences.edges_only_in_old, result.differences.edges_only_in_new) == (1, 1)


def test_failure_report_carries_digests_and_counts_but_no_property_values() -> None:
    result = _diverging_run(_artifact_with_embedding(), _with_ulp_off_embedding)

    rendered = repr(result)

    assert result.old_digest in rendered
    assert result.new_digest in rendered
    for content in ("classified wording", "0.1", "0.3", "p-3"):
        assert content not in rendered


def test_harness_does_not_compare_internal_ids_or_insertion_order() -> None:
    def backwards(artifact: Artifact) -> Artifact:
        return Artifact(nodes=artifact.nodes[::-1], edges=artifact.edges[::-1])

    result = _diverging_run(_artifact_with_embedding(), backwards)

    assert result.passed
