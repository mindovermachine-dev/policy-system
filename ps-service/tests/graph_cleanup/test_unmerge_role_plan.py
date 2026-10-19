"""Planning the unmerge of a Role merge, from a REAL merge's audit row.

The world is what the real `role.merge` executor left in the rig's graph plus the row it recorded,
so the planner reads genuine merge output. A plan exists only when the merge can still be undone;
every conflict is explained and creates no approval.
"""

from __future__ import annotations

from dataclasses import replace
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
)
from graph_cleanup._unmerge_runs import role_unmerge_world, unmerge_plan
from ps_service.graph_cleanup.errors import GraphCleanupValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.graph_cleanup.models import RoleUnmergePlan

_OBL_SINCE = "obl_added_since_merge"


def _plan(rig: CleanupRig) -> RoleUnmergePlan:
    plan = unmerge_plan(rig, ROLE_ABSORBED)
    assert plan.preview.kind == "role"
    return plan  # pyright: ignore[reportReturnType] -- narrowed by the kind assertion


def test_the_preview_names_the_role_to_recreate_and_the_obligations_that_go_back(
    cleanup_rig: CleanupRig,
) -> None:
    role_unmerge_world(cleanup_rig)

    preview = _plan(cleanup_rig).preview

    assert preview.merged_id == ROLE_ABSORBED
    assert preview.merged_name == "market surveillance authorities"
    assert preview.survivor_id == ROLE_SURVIVOR
    assert preview.survivor_name == "Market Surveillance Authority"
    assert preview.instrument_id == INSTRUMENT_ID
    assert preview.merge_approval_id == "approval-1"
    assert preview.obligations_to_restore == (OBL_MOVED_1, OBL_MOVED_2)
    assert preview.defines_source_ref == ABSORBED_SOURCE_REF
    assert preview.survivor_obligations_added_since_merge == 0
    assert preview.state_digest == _plan(cleanup_rig).state_digest


def test_the_plan_snapshots_show_the_marker_before_and_the_recreated_role_after(
    cleanup_rig: CleanupRig,
) -> None:
    role_unmerge_world(cleanup_rig)

    plan = _plan(cleanup_rig)

    assert {(n.label, n.id) for n in plan.before.nodes} == {
        ("Role", ROLE_SURVIVOR),
        ("MergedRole", ROLE_ABSORBED),
    }
    assert {(n.label, n.id) for n in plan.after.nodes} == {
        ("Role", ROLE_SURVIVOR),
        ("Role", ROLE_ABSORBED),
    }
    [recreated] = [n for n in plan.after.nodes if n.id == ROLE_ABSORBED]
    assert recreated.properties == {"name": "market surveillance authorities", "confidence": 0.8}
    assert {(e.rel_type, e.source_id, e.target_id) for e in plan.after.edges} == {
        ("DEFINES", INSTRUMENT_ID, ROLE_ABSORBED),
        ("HAS", ROLE_ABSORBED, OBL_MOVED_1),
        ("HAS", ROLE_ABSORBED, OBL_MOVED_2),
    }
    assert {(e.rel_type, e.source_id, e.target_id) for e in plan.before.edges} == {
        ("HAS", ROLE_SURVIVOR, OBL_MOVED_1),
        ("HAS", ROLE_SURVIVOR, OBL_MOVED_2),
    }


# -- F6: what the survivor gained since the merge stays on it, counted in the preview -----------


def test_obligations_the_survivor_gained_since_the_merge_are_counted_and_explained(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    role_unmerge_world(rig)
    plain = _plan(rig)
    rig.seed_endpoint("Obligation", _OBL_SINCE, text=_OBL_SINCE)
    rig.seed_has(ROLE_SURVIVOR, _OBL_SINCE)

    plan = _plan(rig)

    assert plan.preview.survivor_obligations_added_since_merge == 1
    assert plan.preview.obligations_to_restore == (OBL_MOVED_1, OBL_MOVED_2)
    assert "stay on the survivor" in plan.preview.note
    assert plan.state_digest != plain.state_digest


def test_the_survivors_own_obligations_are_not_counted_as_added_since_the_merge(
    cleanup_rig: CleanupRig,
) -> None:
    role_unmerge_world(cleanup_rig)

    plan = _plan(cleanup_rig)

    assert OBL_OF_SURVIVOR not in plan.preview.obligations_to_restore
    assert plan.preview.survivor_obligations_added_since_merge == 0
    assert "stay on the survivor" in plan.preview.note


def test_losing_an_obligation_the_survivor_bore_never_makes_the_count_negative(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    role_unmerge_world(rig)
    [key] = [k for k in rig.graph.edges if k[0] == "HAS" and k[4] == OBL_OF_SURVIVOR]
    del rig.graph.edges[key]

    assert _plan(rig).preview.survivor_obligations_added_since_merge == 0


# -- conflicts: the live graph no longer fits the merge --------------------------------------


def _marker_points_elsewhere(rig: CleanupRig) -> None:
    rig.graph.nodes[("MergedRole", ROLE_ABSORBED)]["merged_into"] = "role_somewhere_else"


def _marker_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("MergedRole", ROLE_ABSORBED)]


def _absorbed_exists_again(rig: CleanupRig) -> None:
    rig.seed_role(ROLE_ABSORBED, name="reborn")


def _survivor_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("Role", ROLE_SURVIVOR)]


def _instrument_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("RegulatoryInstrument", INSTRUMENT_ID)]


def _moved_obligation_gone(rig: CleanupRig) -> None:
    del rig.graph.nodes[("Obligation", OBL_MOVED_1)]
    for key in [k for k in rig.graph.edges if OBL_MOVED_1 in (k[2], k[4])]:
        del rig.graph.edges[key]


def _moved_obligation_unborne(rig: CleanupRig) -> None:
    [key] = [k for k in rig.graph.edges if k[0] == "HAS" and k[4] == OBL_MOVED_2]
    del rig.graph.edges[key]


def _moved_obligation_taken_by_a_third_role(rig: CleanupRig) -> None:
    rig.seed_role("role_third", name="third")
    rig.seed_has("role_third", OBL_MOVED_1)


@pytest.mark.parametrize(
    ("drift", "fragment"),
    [
        (_marker_points_elsewhere, "marker now points at"),
        (_marker_gone, "no MergedRole marker"),
        (_absorbed_exists_again, "already exists again"),
        (_survivor_gone, "survivor role"),
        (_instrument_gone, "instrument"),
        (_moved_obligation_gone, OBL_MOVED_1),
        (_moved_obligation_unborne, OBL_MOVED_2),
        (_moved_obligation_taken_by_a_third_role, "role_third"),
    ],
    ids=[
        "marker_repointed",
        "marker_gone",
        "absorbed_exists_again",
        "survivor_gone",
        "instrument_gone",
        "moved_obligation_gone",
        "moved_obligation_unborne",
        "moved_obligation_borne_by_a_third_role",
    ],
)
def test_a_conflict_with_the_live_graph_is_refused_with_an_explanation(
    cleanup_rig: CleanupRig, drift: Callable[[CleanupRig], None], fragment: str
) -> None:
    rig = cleanup_rig
    role_unmerge_world(rig)
    drift(rig)

    with pytest.raises(GraphCleanupValidationError, match=fragment):
        _plan(rig)


def test_the_write_parameters_name_every_obligation_the_merge_moved(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    role_unmerge_world(rig)

    assert _plan(rig).write.moved_obligation_ids == (OBL_MOVED_1, OBL_MOVED_2)


# -- the audit record must hold what is needed to reverse --------------------------------------


@pytest.mark.parametrize("missing", ["before", "after", "moved_obligation_ids", "instrument"])
def test_an_audit_record_without_the_snapshot_or_moved_set_cannot_be_reversed(
    cleanup_rig: CleanupRig, missing: str
) -> None:
    rig = cleanup_rig
    role_unmerge_world(rig)
    [event] = rig.audit.history
    details = dict(event.details)
    if missing == "instrument":
        details["instrument_id"] = ""
    else:
        details.pop(missing)
    rig.audit.history = [replace(event, details=details)]

    with pytest.raises(GraphCleanupValidationError, match="cannot be reversed"):
        _plan(rig)
