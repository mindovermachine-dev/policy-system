"""A near-miss merge deletes the loser and marks it, so the loser id is redirected on re-ingest.

Resolving a near miss with "merge" runs the graph-cleanup `merge-capabilities` merge: the loser
node is deleted and a `MergedCapability {id, merged_into}` marker remains, its edges move to the
survivor, and the `PendingReview` is deleted in the same write group. A later ingest or offline
restore that carries the loser id again resolves it to the survivor through
`company_merge.redirects.resolve_redirects`, mints nothing and files no new `PendingReview`, exactly
as after any cleanup merge (`test_redirects.py`, `tests/restore/test_restore_redirects.py`).

The real request (`run_request_near_miss_merge`), signing hand-off and executor
(`execute_capability_merge`) run over the gateway rig; the resolver then reads the rig graph the
merge group built.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from graph_cleanup._fakes import ABSORBED, SURVIVOR, FakeApprovalStore
from graph_cleanup._gateway_fakes import CleanupRig
from graph_cleanup._merge_runs import CONFIG, OFFICER, roles
from graph_gateway._fakes import close_all_rigs

from company_merge._marker_graph import MarkerGraph
from ps_service.api.near_miss_review_orchestration import (
    build_default_near_miss_review_dependencies,
    run_request_near_miss_merge,
)
from ps_service.company_merge.models import BaselineGraph, BaselineNode
from ps_service.company_merge.redirects import resolve_redirects
from ps_service.graph_cleanup.executors import execute_capability_merge
from ps_service.logging import configure

if TYPE_CHECKING:
    from collections.abc import Iterator

    from graph_cleanup._gateway_fakes import CleanupReadGraph

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

    assert outcome == {"survivor_id": SURVIVOR, "absorbed_id": ABSORBED, "merged": True}
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
