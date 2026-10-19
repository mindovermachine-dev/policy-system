"""Pins of the Capability dedup behaviour, on the live and the offline entry point.

Both entry points decide the same way over the same inputs, so each scenario below is stated once
as literal resolutions and near misses and run through both. The offline-only warning and the
live-only embedding backfill have their own cases. A last case drives `merge_baseline_graph` over
the gateway rig and pins the reviews that reach the graph.

The vectors are chosen so their cosine similarities are known: against `(1, 0)`, `(0.96, 0.28)`
scores 0.96 and `(0.6, 0.8)` scores 0.6, `(0.8, 0.6)` scores 0.8 and `(0.9, 0.43589)` scores 0.9.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from company_merge._gateway_fakes import FamilyABaseline, MergeRig, family_a_baseline_graph
from company_merge._seed_merge_graphs import (
    EMBED_MODEL,
    MERGE_THRESHOLD,
    BaselineGraphReader,
    OneHotEmbeddingCaller,
    SingleTenantStore,
)
from company_merge._thresholds import NO_FLOOR_REVIEW_THRESHOLD
from ps_service.company_merge.dedup import (
    dedupe_canonical_nodes,
    resolve_capability_convergence_offline,
)
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.models import (
    BaselineNode,
    CanonicalResolution,
    DedupResult,
    NearMissPair,
)
from ps_service.company_merge.reviewable_kinds import CAPABILITY, ReviewThresholds

if TYPE_CHECKING:
    from collections.abc import Callable

    from litellm.types.utils import EmbeddingResponse

    from company_merge._fakes import MakeEmitter, ReadLines

Vector = tuple[float, ...]

_A: Vector = (1.0, 0.0)
_NEAR: Vector = (0.96, 0.28)
_FAR: Vector = (0.6, 0.8)
_LOW: Vector = (0.8, 0.6)
_MID: Vector = (0.9, 0.43589)


class _NoEmbeddingCaller:
    """An `EmbeddingCaller` that fails the test: every vector in these scenarios is cached."""

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        raise AssertionError(f"an embedding was requested for {inputs!r}")


@dataclass(frozen=True)
class _Scenario:
    """Existing canonical Capabilities, incoming ones and the expected outcome."""

    existing: dict[str, tuple[str, Vector]]
    incoming: tuple[tuple[str, str, Vector], ...]
    resolutions: tuple[tuple[str, str, str], ...]
    near_misses: tuple[tuple[str, str, float], ...]


_SCENARIOS: dict[str, _Scenario] = {
    "exact": _Scenario(
        existing={"cap_a": ("Encrypt data", _A)},
        incoming=(("cap_a", "Encrypt data", _A),),
        resolutions=(("cap_a", "cap_a", "exact"),),
        near_misses=(),
    ),
    "semantic_merge": _Scenario(
        existing={"cap_a": ("Encrypt data", _A)},
        incoming=(("cap_b", "Encrypt all data", _NEAR),),
        resolutions=(("cap_b", "cap_a", "semantic"),),
        near_misses=(),
    ),
    "new_without_neighbours": _Scenario(
        existing={},
        incoming=(("cap_b", "Patch vulnerabilities", _FAR),),
        resolutions=(("cap_b", "cap_b", "new"),),
        near_misses=(),
    ),
    "new_with_a_below_threshold_neighbour": _Scenario(
        existing={"cap_a": ("Encrypt data", _A)},
        incoming=(("cap_b", "Patch vulnerabilities", _FAR),),
        resolutions=(("cap_b", "cap_b", "new"),),
        near_misses=(("cap_b", "cap_a", 0.6),),
    ),
    "merge_with_a_same_run_mint_above_threshold": _Scenario(
        existing={"cap_a": ("Encrypt data", _A)},
        incoming=(
            ("cap_c", "Log events", _LOW),
            ("cap_d", "Record events", _MID),
        ),
        resolutions=(("cap_c", "cap_c", "new"), ("cap_d", "cap_a", "semantic")),
        near_misses=(("cap_c", "cap_a", 0.8), ("cap_d", "cap_c", 0.9815)),
    ),
    "same_run_exact_convergence": _Scenario(
        existing={},
        incoming=(
            ("cap_b", "Patch vulnerabilities", _FAR),
            ("cap_b", "Patch vulnerabilities", _FAR),
        ),
        resolutions=(("cap_b", "cap_b", "new"), ("cap_b", "cap_b", "exact")),
        near_misses=(),
    ),
}


def _single_tenant(scenario: _Scenario) -> SingleTenantStore:
    return SingleTenantStore(
        nodes={
            ("Capability", node_id): {"name": name, "embedding": list(vector)}
            for node_id, (name, vector) in scenario.existing.items()
        }
    )


def _live(scenario: _Scenario) -> DedupResult:
    return dedupe_canonical_nodes(
        tuple(
            BaselineNode(id=node_id, properties={"name": name}, embedding=vector)
            for node_id, name, vector in scenario.incoming
        ),
        kind=CAPABILITY,
        single_tenant_graph=_single_tenant(scenario),
        model=EMBED_MODEL,
        thresholds=ReviewThresholds(merge=MERGE_THRESHOLD),
        call_embedding=_NoEmbeddingCaller(),
    )


def _offline(scenario: _Scenario) -> DedupResult:
    return resolve_capability_convergence_offline(
        tuple(
            BaselineNode(id=node_id, properties={"name": name})
            for node_id, name, _ in scenario.incoming
        ),
        incoming_embeddings={node_id: vector for node_id, _, vector in scenario.incoming},
        single_tenant_graph=_single_tenant(scenario),
        kind=CAPABILITY,
        thresholds=ReviewThresholds(merge=MERGE_THRESHOLD),
    )


_PATHS: dict[str, Callable[[_Scenario], DedupResult]] = {"live": _live, "offline": _offline}


def _resolution_triples(result: DedupResult) -> tuple[tuple[str, str, str], ...]:
    return tuple((r.incoming_id, r.canonical_id, r.match_kind) for r in result.resolutions)


def _assert_near_misses(
    near_misses: tuple[NearMissPair, ...], expected: tuple[tuple[str, str, float], ...]
) -> None:
    assert [(n.incoming_id, n.nearest_existing_id) for n in near_misses] == [
        (incoming, nearest) for incoming, nearest, _ in expected
    ]
    for pair, (_, _, similarity) in zip(near_misses, expected, strict=True):
        assert pair.similarity == pytest.approx(similarity, abs=1e-3)


@pytest.mark.parametrize("path", sorted(_PATHS))
@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_capability_dedup_scenario_resolves_and_surfaces_the_pinned_outcome(
    path: str, name: str
) -> None:
    scenario = _SCENARIOS[name]

    result = _PATHS[path](scenario)

    assert _resolution_triples(result) == scenario.resolutions
    _assert_near_misses(result.near_misses, scenario.near_misses)
    assert result.embedding_backfills == {}
    assert result.incoming_embedding_backfills == {}


@pytest.mark.parametrize("path", sorted(_PATHS))
def test_a_minted_capability_carries_its_own_embedding_and_a_merged_one_none(path: str) -> None:
    result = _PATHS[path](_SCENARIOS["merge_with_a_same_run_mint_above_threshold"])

    assert result.resolutions == (
        CanonicalResolution(
            incoming_id="cap_c", canonical_id="cap_c", match_kind="new", embedding=_LOW
        ),
        CanonicalResolution(
            incoming_id="cap_d", canonical_id="cap_a", match_kind="semantic", embedding=None
        ),
    )


@pytest.mark.parametrize("path", sorted(_PATHS))
def test_a_near_miss_cites_both_texts(path: str) -> None:
    result = _PATHS[path](_SCENARIOS["new_with_a_below_threshold_neighbour"])

    assert [(n.incoming_text, n.nearest_existing_text) for n in result.near_misses] == [
        ("Patch vulnerabilities", "Encrypt data")
    ]


def test_live_dedup_embeds_an_uncached_pair_raises_a_near_miss_without_a_floor_and_backfills(
    make_emitter: MakeEmitter,
) -> None:
    """Orthogonal embeddings score 0.0 and still raise a near miss, whatever the score is."""
    emitter, _log_path = make_emitter()
    graph = SingleTenantStore(nodes={("Capability", "cap_a"): {"name": "Encrypt data"}})

    result = dedupe_canonical_nodes(
        (BaselineNode(id="cap_b", properties={"name": "Patch vulnerabilities"}),),
        kind=CAPABILITY,
        single_tenant_graph=graph,
        model=EMBED_MODEL,
        thresholds=ReviewThresholds(merge=MERGE_THRESHOLD),
        call_embedding=OneHotEmbeddingCaller(),
        emitter=emitter,
    )

    assert _resolution_triples(result) == (("cap_b", "cap_b", "new"),)
    _assert_near_misses(result.near_misses, (("cap_b", "cap_a", 0.0),))
    assert set(result.embedding_backfills) == {"cap_a"}
    assert set(result.incoming_embedding_backfills) == {"cap_b"}


def test_offline_dedup_skips_an_uncached_candidate_and_logs_one_warning(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    graph = SingleTenantStore(nodes={("Capability", "cap_a"): {"name": "Encrypt data"}})

    result = resolve_capability_convergence_offline(
        (BaselineNode(id="cap_b", properties={"name": "Patch vulnerabilities"}),),
        incoming_embeddings={"cap_b": _FAR},
        single_tenant_graph=graph,
        kind=CAPABILITY,
        thresholds=ReviewThresholds(merge=MERGE_THRESHOLD),
        emitter=emitter,
    )

    assert _resolution_triples(result) == (("cap_b", "cap_b", "new"),)
    assert result.near_misses == ()
    emitter.flush()
    warnings = [e for e in read_lines(log_path) if e.get("outcome") == "warning"]
    assert [
        (w["component"], w["action"], w["skipped_missing_embedding_count"]) for w in warnings
    ] == [("company_merge", "resolve_capability_convergence_offline", 1)]


def test_merge_baseline_graph_persists_one_review_per_near_miss_with_the_pair_and_score(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    emitter, _log_path = make_emitter()
    merge_rig.policy_system.seed_node(
        ("Capability",), "capability_old", {"name": "Encrypt data", "confidence": 0.8}
    )
    baseline = FamilyABaseline(
        instrument_id="CRA-1.0",
        instrument_properties={"title": "Cyber Resilience Act"},
        capabilities={"capability_new": ("Patch vulnerabilities", 0.9)},
    )

    result = merge_baseline_graph(
        baseline.instrument_id,
        baseline_graph=BaselineGraphReader(family_a_baseline_graph(baseline)),
        single_tenant_graph=merge_rig.legacy,
        embed_model=EMBED_MODEL,
        merge_threshold=MERGE_THRESHOLD,
        review_threshold=NO_FLOOR_REVIEW_THRESHOLD,
        call_embedding=OneHotEmbeddingCaller(),
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    reviews = [
        props
        for (label, _), props in merge_rig.policy_system.nodes.items()
        if label == "PendingReview"
    ]
    assert [
        (r["kind"], r["incoming_id"], r["nearest_existing_id"], r["similarity"]) for r in reviews
    ] == [("Capability", "capability_new", "capability_old", 0.0)]
    assert [(n.incoming_id, n.nearest_existing_id) for n in result.near_misses] == [
        ("capability_new", "capability_old")
    ]
    assert result.pending_review_count == 1
