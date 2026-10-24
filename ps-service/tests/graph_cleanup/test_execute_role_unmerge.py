"""Executor for `unmerge-entities` of a Role merge, happy path.

The world is what the REAL `role.merge` executor left in the rig's graph and audit history. The
unmerge records its `applied` `role.unmerge` row, then recreates the Role, its `DEFINES` edge and
the moved Obligations' `HAS` edges and deletes the `MergedRole` marker in one gateway group.
Refusals and failures are in `test_execute_role_unmerge_refusals.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from graph_cleanup._fakes import (
    ABSORBED_SOURCE_REF,
    INSTRUMENT_ID,
    OBL_MOVED_1,
    OBL_MOVED_2,
    OBL_OF_SURVIVOR,
    ROLE_ABSORBED,
    ROLE_SURVIVOR,
    SURVIVOR_SOURCE_REF,
)
from graph_cleanup._unmerge_runs import (
    role_unmerge_row,
    role_unmerge_world,
    run_role_unmerge,
)
from ps_service.graph_cleanup.executors import verify_unmerge_effect
from ps_service.logging import configure

if TYPE_CHECKING:
    from graph_cleanup._fakes import RecordedAudit
    from graph_cleanup._gateway_fakes import CleanupRig

_MOVED = [OBL_MOVED_1, OBL_MOVED_2]
_OBL_SINCE = "obl_added_since_merge"


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _has_sources(rig: CleanupRig, obligation_id: str) -> list[str]:
    """Ids of every Role with a `HAS` edge to the Obligation."""
    return [
        key[2]
        for key in rig.graph.edges
        if key[0] == "HAS" and key[1] == "Role" and key[4] == obligation_id
    ]


def _unmerge_rows(rig: CleanupRig) -> list[RecordedAudit]:
    return [r for r in rig.audit.rows if r.action == "role.unmerge"]


def test_happy_path_audits_first_then_writes_once_and_reports_the_unmerge(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    role_unmerge_world(rig)
    row = role_unmerge_row(rig)
    groups_before = len(rig.logged_groups)

    outcome = run_role_unmerge(rig, row=row)

    assert outcome == {
        "merged_id": ROLE_ABSORBED,
        "survivor_id": ROLE_SURVIVOR,
        "unmerged": True,
        "survivor_obligations_added_since_merge": 0,
    }
    assert rig.order()[2:] == ["audit:applied", "log_append"]
    assert len(rig.logged_groups) == groups_before + 1
    [recorded] = _unmerge_rows(rig)
    assert recorded.resource_type == "role"
    assert recorded.resource_id == ROLE_ABSORBED
    assert recorded.outcome == "applied"
    assert (recorded.actor_subject, recorded.actor_issuer) == (
        "officer",
        "https://issuer.example.com/",
    )
    details = recorded.details
    assert details["survivor_id"] == ROLE_SURVIVOR
    assert details["absorbed_id"] == ROLE_ABSORBED
    assert details["instrument_id"] == INSTRUMENT_ID
    assert details["approval_id"] == "approval-2"
    assert details["reverses_approval_id"] == "approval-1"
    assert details["restored_obligation_ids"] == _MOVED
    assert details["survivor_obligations_added_since_merge"] == 0
    assert details["before"] and details["after"]
    assert "reason_code" not in details


def test_the_absorbed_role_is_recreated_with_its_original_properties_and_defines_source_ref(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    role_unmerge_world(rig)

    run_role_unmerge(rig)

    assert rig.graph.nodes[("Role", ROLE_ABSORBED)] == {
        "name": "market surveillance authorities",
        "confidence": 0.8,
    }
    [defines] = [k for k in rig.graph.edges if k[0] == "DEFINES" and k[4] == ROLE_ABSORBED]
    assert defines[2] == INSTRUMENT_ID
    assert rig.graph.edges[defines]["source_ref"] == ABSORBED_SOURCE_REF


def test_exactly_the_moved_obligations_are_borne_by_the_recreated_role_again(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    role_unmerge_world(rig)

    run_role_unmerge(rig)

    for obligation_id in _MOVED:
        assert ("Obligation", obligation_id) in rig.graph.nodes
        assert _has_sources(rig, obligation_id) == [ROLE_ABSORBED]
    assert _has_sources(rig, OBL_OF_SURVIVOR) == [ROLE_SURVIVOR]


def test_the_marker_is_deleted_and_the_survivor_is_untouched(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    role_unmerge_world(rig)
    survivor_before = dict(rig.graph.nodes[("Role", ROLE_SURVIVOR)])

    run_role_unmerge(rig)

    assert ("MergedRole", ROLE_ABSORBED) not in rig.graph.nodes
    assert rig.graph.nodes[("Role", ROLE_SURVIVOR)] == survivor_before
    [survivor_defines] = [k for k in rig.graph.edges if k[0] == "DEFINES" and k[4] == ROLE_SURVIVOR]
    assert rig.graph.edges[survivor_defines]["source_ref"] == SURVIVOR_SOURCE_REF


# -- F9: the restored edges carry the standard identities so a re-ingest collapses onto them ----


def test_restored_edges_carry_the_standard_edge_identities(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    role_unmerge_world(rig)

    run_role_unmerge(rig)

    identities = {(k[0], k[2], k[4]): k[5] for k in rig.graph.edges}
    assert identities[("DEFINES", INSTRUMENT_ID, ROLE_ABSORBED)] == (
        f"DEFINES:{INSTRUMENT_ID}->{ROLE_ABSORBED}"
    )
    for obligation_id in _MOVED:
        assert identities[("HAS", ROLE_ABSORBED, obligation_id)] == (
            f"HAS:{ROLE_ABSORBED}->{obligation_id}"
        )
    survivor_has = [k[4] for k in rig.graph.edges if k[0] == "HAS" and k[2] == ROLE_SURVIVOR]
    assert survivor_has == [OBL_OF_SURVIVOR]


def test_one_edge_per_pair_remains_after_the_round_trip(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    role_unmerge_world(rig)

    run_role_unmerge(rig)

    edges = sorted((k[0], k[2], k[4]) for k in rig.graph.edges)
    assert edges == sorted(set(edges))
    assert len([e for e in edges if e[0] == "HAS"]) == 3
    assert len([e for e in edges if e[0] == "DEFINES"]) == 2


# -- F6: what the survivor gained since the merge stays on it ----------------------------------


def test_an_obligation_the_survivor_gained_since_the_merge_stays_on_the_survivor(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    role_unmerge_world(rig)
    rig.seed_endpoint("Obligation", _OBL_SINCE, text=_OBL_SINCE)
    rig.seed_has(ROLE_SURVIVOR, _OBL_SINCE)
    row = role_unmerge_row(rig)
    assert row.display_summary["survivor_obligations_added_since_merge"] == 1

    outcome = run_role_unmerge(rig, row=row)

    assert outcome["survivor_obligations_added_since_merge"] == 1
    assert _has_sources(rig, _OBL_SINCE) == [ROLE_SURVIVOR]
    [recorded] = _unmerge_rows(rig)
    assert recorded.details["survivor_obligations_added_since_merge"] == 1
    assert recorded.details["restored_obligation_ids"] == _MOVED


# -- the effect verifier (orphan reconciliation) -----------------------------------------------


def test_the_effect_verifier_sees_the_unmerge_only_after_it_ran(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    role_unmerge_world(rig)
    row = role_unmerge_row(rig)

    before = verify_unmerge_effect(row, rig.read)
    run_role_unmerge(rig, row=row)
    after = verify_unmerge_effect(row, rig.read)

    assert (before, after) == (False, True)


def test_a_merge_again_after_an_unmerge_is_planned_like_the_first(cleanup_rig: CleanupRig) -> None:
    from graph_cleanup._merge_runs import run_role_merge

    rig = cleanup_rig
    role_unmerge_world(rig)
    run_role_unmerge(rig)

    outcome = run_role_merge(rig)

    assert outcome == {"survivor_id": ROLE_SURVIVOR, "absorbed_id": ROLE_ABSORBED, "merged": True}
    for obligation_id in _MOVED:
        assert _has_sources(rig, obligation_id) == [ROLE_SURVIVOR]
