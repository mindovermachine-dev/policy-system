"""A split whose graph changed between the preview and the signature writes nothing.

The approval binds the state digest of the previewed state. At sign time the executor re-reads the
policy graph and the baselines: a marker that no longer validates is `revalidation_rejected`, a
different state is `state_digest_mismatch`, and a change between the write step's guard read and
the submit is `graph_guard_missed`. Each records one `failed` `capability.split` row with the
approval's id and leaves every node, edge and the graph log as they were.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from graph_cleanup._gateway_fakes import SPLIT_A, SPLIT_X
from graph_cleanup._split_runs import (
    SPLIT_ACTION,
    graph_state,
    only_failed_details,
    run_capability_split,
    split_row,
    split_rows,
)
from ps_service.graph_cleanup import graph_reader

if TYPE_CHECKING:
    from collections.abc import Callable

    from graph_cleanup._gateway_fakes import CleanupRig

_STALE = {
    "error": "the graph changed since the preview; nothing was changed, ask for a new approval"
}
_X_PRESENT_READ = graph_reader._CAPABILITY_PRESENT_QUERY  # pyright: ignore[reportPrivateUsage]
"""Read once by the executor's re-plan and once by the write step's guard."""


def _world(rig: CleanupRig) -> None:
    rig.seed_capability_split_world(governed=True)


def _marker_deleted(rig: CleanupRig) -> None:
    del rig.graph.nodes[("ResolvedCapability", SPLIT_X)]


def _x_exists_as_a_capability(rig: CleanupRig) -> None:
    rig.seed_capability(SPLIT_X, name="Vulnerability patching", status="active")


def _x_replaced_by_a_cleanup_merge_marker(rig: CleanupRig) -> None:
    del rig.graph.nodes[("ResolvedCapability", SPLIT_X)]
    rig.graph.seed_node(("MergedCapability",), SPLIT_X, {"merged_into": SPLIT_A})


def _a_merged_away(rig: CleanupRig) -> None:
    rig.seed_capability("cap_z_survivor", name="Vulnerability management", status="active")
    del rig.graph.nodes[("Capability", SPLIT_A)]
    rig.graph.seed_node(("MergedCapability",), SPLIT_A, {"merged_into": "cap_z_survivor"})


def _baseline_gains_an_obligation_requiring_x(rig: CleanupRig) -> None:
    rig.seed_endpoint("Obligation", "obl_since", text="obl_since")
    rig.seed_baseline_requires("nis2_baseline", (("obl_since", SPLIT_X),))


def _a_loses_an_edge(rig: CleanupRig) -> None:
    [key] = [
        k for k in rig.graph.edges if k[0] == "REQUIRES" and k[2] == "obl_stays" and k[4] == SPLIT_A
    ]
    del rig.graph.edges[key]


def test_marker_deleted_between_preview_and_signing_writes_nothing_and_one_failed_row(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    row = split_row(rig)
    _marker_deleted(rig)
    before = graph_state(rig)
    groups = len(rig.store.groups)

    outcome = run_capability_split(rig, row=row)

    assert outcome == {"error": f"there is no automatic resolution of capability {SPLIT_X!r}"}
    assert graph_state(rig) == before
    assert len(rig.store.groups) == groups
    details = only_failed_details(rig, row)
    assert details["reason_code"] == "revalidation_rejected"
    assert (details["x_id"], details["a_id"]) == (SPLIT_X, SPLIT_A)


@pytest.mark.parametrize(
    "drift",
    [_x_exists_as_a_capability, _x_replaced_by_a_cleanup_merge_marker],
    ids=["x_exists_as_a_capability", "x_merged_by_a_cleanup_merge"],
)
def test_an_x_that_no_longer_validates_is_rejected_with_one_failed_row(
    cleanup_rig: CleanupRig, drift: Callable[[CleanupRig], None]
) -> None:
    rig = cleanup_rig
    _world(rig)
    row = split_row(rig)
    drift(rig)
    before = graph_state(rig)
    groups = len(rig.store.groups)

    outcome = run_capability_split(rig, row=row)

    assert "error" in outcome
    assert graph_state(rig) == before
    assert len(rig.store.groups) == groups
    assert only_failed_details(rig, row)["reason_code"] == "revalidation_rejected"


@pytest.mark.parametrize(
    "drift",
    [_a_merged_away, _baseline_gains_an_obligation_requiring_x, _a_loses_an_edge],
    ids=["a_merged_away", "baseline_gains_an_obligation_requiring_x", "a_loses_an_edge"],
)
def test_a_changed_state_is_refused_stale_with_one_failed_row_and_no_change(
    cleanup_rig: CleanupRig, drift: Callable[[CleanupRig], None]
) -> None:
    rig = cleanup_rig
    _world(rig)
    row = split_row(rig)
    drift(rig)
    before = graph_state(rig)
    groups = len(rig.store.groups)

    outcome = run_capability_split(rig, row=row)

    assert outcome == _STALE
    assert graph_state(rig) == before
    assert len(rig.store.groups) == groups
    assert only_failed_details(rig, row)["reason_code"] == "state_digest_mismatch"


def test_a_change_before_the_guard_read_is_a_missed_guard_after_the_applied_row(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    row = split_row(rig)
    groups = len(rig.store.groups)
    rig.read.flip_before(_X_PRESENT_READ, 2, lambda: _a_loses_an_edge(rig))

    outcome = run_capability_split(rig, row=row)

    assert outcome == _STALE
    assert [(r.action, r.outcome) for r in split_rows(rig)] == [
        (SPLIT_ACTION, "applied"),
        (SPLIT_ACTION, "failed"),
    ]
    applied, failed = split_rows(rig)
    assert failed.details["reason_code"] == "graph_guard_missed"
    assert failed.details["approval_id"] == applied.details["approval_id"] == row.id
    assert len(rig.store.groups) == groups
    assert ("ResolvedCapability", SPLIT_X) in rig.graph.nodes
    assert ("Capability", SPLIT_X) not in rig.graph.nodes


def test_a_log_that_moves_before_the_submit_is_a_missed_guard_with_nothing_of_the_split_logged(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    row = split_row(rig)
    rig.hooked.before_submit(rig.append_unrelated_group)
    before = graph_state(rig)

    outcome = run_capability_split(rig, row=row)

    assert outcome == _STALE
    assert [(r.action, r.outcome) for r in split_rows(rig)] == [
        (SPLIT_ACTION, "applied"),
        (SPLIT_ACTION, "failed"),
    ]
    assert split_rows(rig)[-1].details["reason_code"] == "graph_guard_missed"
    assert rig.cleanup_groups() == []
    nodes_before, edges_before = before
    nodes_after, edges_after = graph_state(rig)
    assert edges_after == edges_before
    assert {k: v for k, v in nodes_after.items() if k != ("Capability", "cap_unrelated")} == (
        nodes_before
    )
