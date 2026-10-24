"""A near-miss merge deletes the loser and marks it, so the loser id is redirected on re-ingest.

Merging a near miss in a batch of one runs the graph-cleanup Capability merge: the loser
node is deleted and a `MergedCapability {id, merged_into}` marker remains, its edges move to the
survivor, and the `PendingReview` is deleted in the same write group. A later ingest or offline
restore that carries the loser id again resolves it to the survivor through
`company_merge.redirects.resolve_redirects`, mints nothing and files no new `PendingReview`, exactly
as after any cleanup merge (`test_redirects.py`, `tests/restore/test_restore_redirects.py`).

The real batch request (`create_near_miss_merge_batch`), signing hand-off and batch executor run
over the gateway rig; the resolver then reads the rig graph the
merge group built.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from graph_cleanup._fakes import ABSORBED, SURVIVOR, FakeApprovalStore
from graph_cleanup._gateway_fakes import CleanupRig
from graph_cleanup._merge_runs import CONFIG, OFFICER, roles
from graph_gateway._fakes import close_all_rigs

from company_merge._marker_graph import MarkerGraph
from ps_service.company_merge.models import BaselineGraph, BaselineNode
from ps_service.company_merge.redirects import resolve_redirects
from ps_service.graph_cleanup.near_miss_batch import create_near_miss_merge_batch
from ps_service.graph_cleanup.near_miss_batch_execution import execute_near_miss_batch
from ps_service.logging import configure

if TYPE_CHECKING:
    from collections.abc import Iterator

_REVIEW = "review_aaa"
_BASE_URL = "https://ps.example.com"


@pytest.fixture(autouse=True)
def _logging_configured() -> None:  # pyright: ignore[reportUnusedFunction]  # autouse fixture
    configure()


@pytest.fixture
def rig() -> Iterator[CleanupRig]:
    """A cleanup rig whose gateway reconciler is stopped after the test."""
    yield CleanupRig()
    close_all_rigs()


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

    approval = create_near_miss_merge_batch(
        rig.read,
        review_ids=[_REVIEW],
        acknowledged_review_ids=[],
        actor=OFFICER,
        base_url=_BASE_URL,
        store=store,
    )
    assert store.mark_signed(approval.pending_approval_id)
    signed = store.get_by_id(approval.pending_approval_id)
    assert signed is not None
    return execute_near_miss_batch(signed, CONFIG, rig.dependencies(roles()))


def _loser_as_baseline() -> BaselineGraph:
    return BaselineGraph(
        regulatory_instrument_id="reg_1",
        regulatory_instrument_properties={"id": "reg_1"},
        role_nodes=(),
        requirement_nodes=(),
        obligation_nodes=(),
        capability_nodes=(
            BaselineNode(id=ABSORBED, properties={"name": "Absorbed", "confidence": 0.9}),
        ),
        provenance_edges=(),
        bare_edges=(),
    )


def test_a_near_miss_merge_deletes_the_loser_leaves_a_marker_and_deletes_the_review(
    rig: CleanupRig,
) -> None:
    outcome = _merge_the_near_miss(rig)

    [merge] = cast("list[dict[str, object]]", outcome["merges"])
    assert (merge["state"], merge["winner_id"], merge["loser_id"]) == ("merged", SURVIVOR, ABSORBED)
    assert ("Capability", ABSORBED) not in rig.graph.nodes
    assert rig.graph.nodes[("MergedCapability", ABSORBED)] == {"merged_into": SURVIVOR}
    assert ("PendingReview", _REVIEW) not in rig.graph.nodes


def test_ingest_and_restore_of_the_loser_id_resolve_to_the_survivor_and_mint_nothing(
    rig: CleanupRig,
) -> None:
    _merge_the_near_miss(rig)

    resolution = resolve_redirects(MarkerGraph(gateway_graph=rig.graph), _loser_as_baseline())

    assert resolution.mapping == {ABSORBED: SURVIVOR}
    assert resolution.baseline.capability_nodes == ()
    assert ("Capability", ABSORBED) not in rig.graph.nodes
    assert not [k for k in rig.graph.nodes if k[0] == "PendingReview"]
