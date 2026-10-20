"""Ingest and restore raise the same near misses at the same review floor.

The same three incoming Capabilities (`_review_floor_fixtures`) go through the live merge
(`merge_baseline_graph`) and through restore (`restore_instrument`, the offline detector), each
into its own `policy_system` holding the same three Capabilities. For every floor tried, the
reviews each leaves behind are the same pairs with the same scores.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from company_merge._gateway_fakes import MergeRig, family_a_baseline_graph
from company_merge._review_floor_fixtures import (
    EMBED_MODEL,
    MERGE,
    ByNameEmbedding,
    ingest_baseline,
    restore_artifact,
    seed_existing,
)
from company_merge._seed_merge_graphs import BaselineGraphReader
from company_merge._thresholds import ROLE_REVIEW_THRESHOLD
from graph_gateway._fakes import close_all_rigs

from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.logging import configure
from restore._gateway_fakes import RestoreRig

if TYPE_CHECKING:
    from collections.abc import Iterator

    from company_merge._fakes import MakeEmitter

Pair = tuple[object, object, object]


def _pairs(nodes: dict[tuple[str, str], dict[str, object]]) -> set[Pair]:
    return {
        (p["incoming_id"], p["nearest_existing_id"], p["similarity"])
        for (label, _), p in nodes.items()
        if label == "PendingReview"
    }


@pytest.fixture
def ingest_rig() -> Iterator[MergeRig]:
    rig = MergeRig()
    yield rig
    close_all_rigs()


@pytest.fixture
def restore_rig_() -> Iterator[RestoreRig]:
    configure()  # the restore logs through the process default emitter
    rig = RestoreRig()
    yield rig
    close_all_rigs()


@pytest.mark.parametrize("floor", [0.0, 0.35, 0.5, 0.55, 0.9])
def test_AC_BI_010_ingest_and_restore_raise_identical_near_misses(
    floor: float, ingest_rig: MergeRig, restore_rig_: RestoreRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    baseline = ingest_baseline()
    seed_existing(ingest_rig.policy_system)
    seed_existing(restore_rig_.policy_system)

    merge_baseline_graph(
        baseline.instrument_id,
        baseline_graph=BaselineGraphReader(family_a_baseline_graph(baseline)),
        single_tenant_graph=ingest_rig.legacy,
        embed_model=EMBED_MODEL,
        merge_threshold=MERGE,
        review_threshold=floor,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=ByNameEmbedding(),
        emitter=emitter,
        **ingest_rig.merge_keywords(),
    )
    restore_rig_.restore(restore_artifact(), threshold=MERGE, review_threshold=floor)

    ingested = _pairs(ingest_rig.policy_system.nodes)
    restored = _pairs(restore_rig_.graph("policy_system").nodes)
    assert ingested == restored
    assert len(ingested) == {0.0: 3, 0.35: 3, 0.5: 2, 0.55: 1, 0.9: 0}[floor]
