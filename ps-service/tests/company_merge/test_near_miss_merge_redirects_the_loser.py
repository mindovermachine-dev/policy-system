"""A near-miss merge tombstones the loser, so the loser id is redirected on the next ingest.

Resolving a near miss with "merge" runs the graph-cleanup `merge-capabilities` merge: the loser
stays in the graph as a `merged` tombstone with one `MERGED_INTO` edge to the survivor, its edges
move to the survivor, and the `PendingReview` is deleted in the same write group. A later ingest or
offline restore that carries the loser id again resolves it to the survivor ("redirected"), mints
nothing and files no new `PendingReview`, exactly as after any cleanup merge
(`test_dedup_merged_redirect.py`, `test_dedup_offline_merged_redirect.py`).

The real request (`run_request_near_miss_merge`), signing hand-off and executor
(`execute_capability_merge`) run over the gateway rig; the live and offline convergence functions
then read the rig graph the merge group built. The first red state of this file was the former
`test_near_miss_merge_leaves_no_redirect.py`, which pinned the opposite outcome of the legacy
near-miss write (loser removed, loser id minted again); that write is the path this slice retired
from the merge flow. These tests characterize the already-implemented cleanup path and are not
red against any code still in the tree.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest
from graph_cleanup._fakes import ABSORBED, SURVIVOR, FakeApprovalStore
from graph_cleanup._gateway_fakes import CleanupRig
from graph_cleanup._merge_runs import CONFIG, OFFICER, roles
from graph_gateway._fakes import close_all_rigs

from ps_service.api.near_miss_review_orchestration import (
    build_default_near_miss_review_dependencies,
    run_request_near_miss_merge,
)
from ps_service.company_merge.dedup import (
    dedupe_canonical_nodes,
    resolve_capability_convergence_offline,
)
from ps_service.company_merge.models import BaselineNode
from ps_service.graph_cleanup.executors import execute_capability_merge
from ps_service.logging import configure

if TYPE_CHECKING:
    from collections.abc import Iterator

    from graph_cleanup._gateway_fakes import CleanupReadGraph

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.models import DedupResult

_REVIEW = "review_aaa"
_THRESHOLD = 0.85
_BASE_URL = "https://ps.example.com"
_SURVIVOR_EMBEDDING = [1.0, 0.0]
_LOSER_EMBEDDING = (0.8, 0.6)
"""Scores 0.8 against the survivor: below `_THRESHOLD`, as it did when the pair was first filed."""


@pytest.fixture(autouse=True)
def _logging_configured() -> None:  # pyright: ignore[reportUnusedFunction]  # autouse fixture
    configure()


@pytest.fixture
def rig() -> Iterator[CleanupRig]:
    """A cleanup rig whose gateway reconciler is stopped after the test."""
    yield CleanupRig()
    close_all_rigs()


class _Result:
    def __init__(self, result_set: list[object]) -> None:
        self.result_set = result_set


class _GraphAfterMerge:
    """Dedup read handle over the Capability nodes and `MERGED_INTO` edges the merge left."""

    def __init__(self, rig: CleanupRig) -> None:
        self._rig = rig

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del params
        nodes = self._rig.graph.nodes
        if "MERGED_INTO" in q:
            redirects = {k[2]: k[4] for k in self._rig.graph.edges if k[0] == "MERGED_INTO"}
            return _Result(
                [
                    [node_id, redirects.get(node_id)]
                    for (label, node_id), props in nodes.items()
                    if label == "Capability" and props.get("status") == "merged"
                ]
            )
        assert "(n:Capability) RETURN" in q, q
        return _Result(
            [
                [node_id, props["name"], _SURVIVOR_EMBEDDING]
                for (label, node_id), props in nodes.items()
                if label == "Capability" and props.get("status") != "merged"
            ]
        )


def _merge_the_near_miss(rig: CleanupRig) -> dict[str, object]:
    """File the review for the pair, request the merge, sign the approval and execute it."""
    rig.seed_capability_merge_world()
    rig.seed_near_miss_review(
        _REVIEW,
        incoming_id=ABSORBED,
        existing_id=SURVIVOR,
        incoming_created_at="2024-01-01T00:00:00+00:00",
        existing_created_at="2020-01-01T00:00:00+00:00",
    )
    store = FakeApprovalStore()

    def _open(_config: object) -> CleanupReadGraph:
        return rig.read

    dependencies = replace(
        build_default_near_miss_review_dependencies(), open_single_tenant_graph=_open
    )
    approval = run_request_near_miss_merge(
        _REVIEW,
        actor=OFFICER,
        acknowledge_governance_change=False,
        base_url=_BASE_URL,
        config=CONFIG,
        dependencies=dependencies,
        store=store,
    )
    assert store.mark_signed(approval.pending_approval_id)
    signed = store.get_by_id(approval.pending_approval_id)
    assert signed is not None
    return execute_capability_merge(signed, CONFIG, rig.dependencies(roles()))


def _loser_as_baseline() -> tuple[BaselineNode, ...]:
    return (BaselineNode(id=ABSORBED, properties={"name": "Absorbed", "confidence": 0.9}),)


def _redirected(result: DedupResult) -> list[tuple[str, str, str]]:
    return [(r.incoming_id, r.canonical_id, r.match_kind) for r in result.resolutions]


def test_a_near_miss_merge_tombstones_the_loser_and_deletes_the_review(rig: CleanupRig) -> None:
    outcome = _merge_the_near_miss(rig)

    assert outcome == {"survivor_id": SURVIVOR, "absorbed_id": ABSORBED, "merged": True}
    # The loser is kept as a tombstone, not deleted, with exactly one redirect edge.
    assert rig.graph.nodes[("Capability", ABSORBED)]["status"] == "merged"
    merged_into = [key for key in rig.graph.edges if key[0] == "MERGED_INTO"]
    assert [(key[2], key[4]) for key in merged_into] == [(ABSORBED, SURVIVOR)]
    assert ("PendingReview", _REVIEW) not in rig.graph.nodes


def test_live_ingest_of_the_loser_id_is_redirected_to_the_survivor_and_mints_nothing(
    rig: CleanupRig,
) -> None:
    _merge_the_near_miss(rig)

    result = dedupe_canonical_nodes(
        _loser_as_baseline(),
        kind="Capability",
        single_tenant_graph=cast("GraphHandle", _GraphAfterMerge(rig)),
        model="fake-embed-model",
        threshold=_THRESHOLD,
    )

    assert _redirected(result) == [(ABSORBED, SURVIVOR, "redirected")]
    assert result.near_misses == ()
    assert ("PendingReview", _REVIEW) not in rig.graph.nodes


def test_offline_restore_of_the_loser_id_is_redirected_to_the_survivor_with_no_near_miss(
    rig: CleanupRig,
) -> None:
    _merge_the_near_miss(rig)

    result = resolve_capability_convergence_offline(
        _loser_as_baseline(),
        incoming_embeddings={ABSORBED: _LOSER_EMBEDDING},
        single_tenant_graph=cast("GraphHandle", _GraphAfterMerge(rig)),
        threshold=_THRESHOLD,
    )

    assert _redirected(result) == [(ABSORBED, SURVIVOR, "redirected")]
    assert result.near_misses == ()
