# pyright: reportPrivateUsage=false
"""A Compliance Officer merge or unmerge of a resolution's target moves where the next ingest lands.

Company Merge resolves Capability `X` onto `ABSORBED` by similarity and records it as a
`ResolvedCapability {id: X, resolves_to: ABSORBED}` marker. A cleanup merge of `ABSORBED` into
`SURVIVOR` then leaves a `MergedCapability` marker, so the chain from `X` ends at `SURVIVOR`; its
unmerge removes that marker and recreates `ABSORBED`, so the chain ends at `ABSORBED` again. The
resolution marker itself is never touched: it is not a merge marker, the cleanup guards and
planners read only `MergedCapability` by label, and so it is never unmerged.

The real cleanup merge and unmerge executors run over the `CleanupRig`, and the real
`merge_baseline_graph` then ingests over the same rig graph, so the tests read where the new
Obligation's `REQUIRES` edge lands rather than a resolver's mapping.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from graph_cleanup._fakes import ABSORBED, SURVIVOR
from graph_cleanup._gateway_fakes import CleanupRig
from graph_cleanup._merge_runs import run_capability_merge
from graph_cleanup._unmerge_runs import (
    capability_unmerge_world,
    history_of_rows,
    run_capability_unmerge,
    unmerge_plan,
    unmerge_row,
)
from graph_gateway._fakes import close_all_rigs

from company_merge._gateway_fakes import POLICY_SYSTEM, RigReadHandle, family_a_baseline_graph
from company_merge._resolution_fixtures import (
    EMBED_MODEL,
    MERGE,
    RESOLVED_ID,
    RESOLVED_NAME,
    obligation_of,
    requiring_baseline,
)
from company_merge._seed_merge_graphs import BaselineGraphReader, OneHotEmbeddingCaller
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.graph_reader import (
    read_capability_present_without_marker,
    read_capability_unmerge_state,
)
from ps_service.graph_cleanup.models import CapabilityUnmergePlan
from ps_service.logging import configure

if TYPE_CHECKING:
    from collections.abc import Iterator

    from company_merge._gateway_fakes import FamilyABaseline

_RESOLUTION = "ResolvedCapability"
_BASELINE_GRAPH = "resolution_baseline"
_RECORDED_RESOLUTION = {"resolves_to": ABSORBED}


@pytest.fixture(autouse=True)
def _logging_configured() -> None:  # pyright: ignore[reportUnusedFunction]  # autouse fixture
    configure()


@pytest.fixture
def rig() -> Iterator[CleanupRig]:
    """A cleanup rig whose gateway reconciler is stopped after the test."""
    yield CleanupRig()
    close_all_rigs()


def _version(instrument_id: str) -> FamilyABaseline:
    """A regulation version whose one Obligation `REQUIRES` the resolved Capability `X`."""
    return requiring_baseline(instrument_id, RESOLVED_ID, RESOLVED_NAME)


def _ingest_over(rig: CleanupRig, baseline: FamilyABaseline) -> None:
    """Run the real Company Merge of `baseline` into the rig's `policy_system` graph."""
    audit_event_id = str(uuid.uuid4())
    rig.store.audit_rows.add(audit_event_id)
    merge_baseline_graph(
        baseline.instrument_id,
        baseline_graph=BaselineGraphReader(family_a_baseline_graph(baseline)),
        single_tenant_graph=RigReadHandle(rig.graph),
        submitter=rig.gateway,
        single_tenant_graph_name=POLICY_SYSTEM,
        baseline_graph_name=_BASELINE_GRAPH,
        audit_event_id=audit_event_id,
        embed_model=EMBED_MODEL,
        merge_threshold=MERGE,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=OneHotEmbeddingCaller(),
    )


def _resolution_world(rig: CleanupRig) -> None:
    """`X` resolved onto `ABSORBED`, then the default capability-merge fixture."""
    rig.seed_endpoint(_RESOLUTION, RESOLVED_ID, **_RECORDED_RESOLUTION)
    rig.seed_capability_merge_world()


def _merge(rig: CleanupRig) -> None:
    """Merge `ABSORBED` into `SURVIVOR` and keep its audit row as the history unmerge plans from."""
    assert run_capability_merge(rig)["merged"] is True
    rig.audit.history = history_of_rows(rig)


def _unmerge(rig: CleanupRig) -> dict[str, object]:
    """Unmerge `ABSORBED`; the approval names the id `run_capability_merge` signed with."""
    return run_capability_unmerge(
        rig, row=unmerge_row(rig, ABSORBED, merge_approval_id="approval-1")
    )


def _requires_targets_of(rig: CleanupRig, obligation: str) -> set[str]:
    return {key[4] for key in rig.graph.edges if key[0] == "REQUIRES" and key[2] == obligation}


def test_an_ingest_after_the_cleanup_merge_lands_a_resolved_capability_on_the_survivor(
    rig: CleanupRig,
) -> None:
    _resolution_world(rig)
    _merge(rig)
    version = _version("RES-2.0")

    _ingest_over(rig, version)

    assert _requires_targets_of(rig, obligation_of(version)) == {SURVIVOR}
    assert ("Capability", RESOLVED_ID) not in rig.graph.nodes
    assert rig.graph.nodes[(_RESOLUTION, RESOLVED_ID)] == _RECORDED_RESOLUTION


def test_unmerging_the_target_keeps_the_resolution_and_the_next_ingest_lands_on_it_again(
    rig: CleanupRig,
) -> None:
    _resolution_world(rig)
    _merge(rig)

    outcome = _unmerge(rig)
    version = _version("RES-2.0")
    _ingest_over(rig, version)

    assert outcome["unmerged"] is True
    assert ("MergedCapability", ABSORBED) not in rig.graph.nodes
    assert rig.graph.nodes[(_RESOLUTION, RESOLVED_ID)] == _RECORDED_RESOLUTION
    assert _requires_targets_of(rig, obligation_of(version)) == {ABSORBED}
    assert ("Capability", RESOLVED_ID) not in rig.graph.nodes


def test_an_ingest_between_merge_and_unmerge_keeps_its_edge_on_the_survivor(
    rig: CleanupRig,
) -> None:
    _resolution_world(rig)
    _merge(rig)
    during_merge = _version("RES-2.0")
    _ingest_over(rig, during_merge)

    outcome = _unmerge(rig)
    after_unmerge = _version("RES-3.0")
    _ingest_over(rig, after_unmerge)

    assert outcome["unmerged"] is True
    assert _requires_targets_of(rig, obligation_of(during_merge)) == {SURVIVOR}
    assert _requires_targets_of(rig, obligation_of(after_unmerge)) == {ABSORBED}
    assert rig.graph.nodes[(_RESOLUTION, RESOLVED_ID)] == _RECORDED_RESOLUTION


def test_unmerge_refuses_an_id_that_only_has_a_resolution_marker(rig: CleanupRig) -> None:
    capability_unmerge_world(rig)
    del rig.graph.nodes[("MergedCapability", ABSORBED)]
    rig.seed_endpoint(_RESOLUTION, ABSORBED, resolves_to=SURVIVOR)

    with pytest.raises(GraphCleanupValidationError, match="has no MergedCapability marker"):
        unmerge_plan(rig, ABSORBED)

    assert rig.graph.nodes[(_RESOLUTION, ABSORBED)] == {"resolves_to": SURVIVOR}
    assert ("Capability", ABSORBED) not in rig.graph.nodes
    assert rig.logged_groups == []


def test_the_merge_guard_does_not_read_a_resolution_marker_as_a_merge_marker(
    rig: CleanupRig,
) -> None:
    capability_unmerge_world(rig)
    rig.seed_endpoint(_RESOLUTION, SURVIVOR, resolves_to=ABSORBED)
    plan = unmerge_plan(rig, ABSORBED)
    assert isinstance(plan, CapabilityUnmergePlan)

    present_without_marker = read_capability_present_without_marker(
        rig.read, capability_id=SURVIVOR
    )
    state = read_capability_unmerge_state(rig.read, inputs=plan.inputs)

    assert present_without_marker is True
    assert state.marker_targets == {ABSORBED: (SURVIVOR,)}
