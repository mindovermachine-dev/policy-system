"""The `merge-roles` preview lists the Obligation pairs the merge would put under one Role.

An absorbed Obligation whose text is identical or near-identical (the `find-duplicate-obligations`
rule) to a survivor Obligation's becomes a duplicate pair once the merge moves it. The preview
names those pairs so the officer can follow up with `merge-obligations`; after the merge
`find-duplicate-obligations` on the survivor returns them as groups.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from graph_cleanup._fakes import (
    OBL_MOVED_1,
    OBL_MOVED_2,
    OBL_OF_SURVIVOR,
    ROLE_ABSORBED,
    ROLE_SURVIVOR,
)
from graph_cleanup._merge_runs import role_plan, run_role_merge
from ps_service.graph_cleanup.service import find_duplicate_obligations, preview_role_merge

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig

_TEXT = "Notify the market surveillance authority of the incident"


def _set_text(rig: CleanupRig, obligation_id: str, text: str) -> None:
    rig.graph.nodes[("Obligation", obligation_id)]["text"] = text


def test_no_pairs_are_listed_when_no_texts_look_alike(cleanup_rig: CleanupRig) -> None:
    cleanup_rig.seed_role_merge_world()

    assert (
        preview_role_merge(
            cleanup_rig.read, survivor_id=ROLE_SURVIVOR, absorbed_id=ROLE_ABSORBED
        ).duplicate_obligation_pairs
        == ()
    )


def test_identical_text_is_listed_as_a_pair(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    _set_text(rig, OBL_OF_SURVIVOR, _TEXT)
    _set_text(rig, OBL_MOVED_1, "notify the market surveillance  authority of the incident.")

    [pair] = preview_role_merge(
        rig.read, survivor_id=ROLE_SURVIVOR, absorbed_id=ROLE_ABSORBED
    ).duplicate_obligation_pairs

    assert pair.survivor_obligation_id == OBL_OF_SURVIVOR
    assert pair.survivor_text == _TEXT
    assert pair.absorbed_obligation_id == OBL_MOVED_1
    assert pair.absorbed_text == "notify the market surveillance  authority of the incident."
    assert pair.basis == "identical_text"


def test_near_identical_text_is_listed_as_a_near_text_pair(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    _set_text(rig, OBL_OF_SURVIVOR, _TEXT)
    _set_text(rig, OBL_MOVED_2, "Notify the market surveillance authority of the major incident")

    [pair] = preview_role_merge(
        rig.read, survivor_id=ROLE_SURVIVOR, absorbed_id=ROLE_ABSORBED
    ).duplicate_obligation_pairs

    assert (pair.survivor_obligation_id, pair.absorbed_obligation_id) == (
        OBL_OF_SURVIVOR,
        OBL_MOVED_2,
    )
    assert pair.basis == "near_text"


def test_duplicates_within_one_side_are_not_pairs_of_the_merge(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    _set_text(rig, OBL_MOVED_1, _TEXT)
    _set_text(rig, OBL_MOVED_2, _TEXT)

    preview = preview_role_merge(rig.read, survivor_id=ROLE_SURVIVOR, absorbed_id=ROLE_ABSORBED)

    assert preview.duplicate_obligation_pairs == ()


def test_every_absorbed_and_survivor_match_is_a_pair_in_a_stable_order(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    for obligation_id in (OBL_OF_SURVIVOR, OBL_MOVED_1, OBL_MOVED_2):
        _set_text(rig, obligation_id, _TEXT)

    preview = preview_role_merge(rig.read, survivor_id=ROLE_SURVIVOR, absorbed_id=ROLE_ABSORBED)

    assert [
        (p.survivor_obligation_id, p.absorbed_obligation_id)
        for p in preview.duplicate_obligation_pairs
    ] == [(OBL_OF_SURVIVOR, OBL_MOVED_1), (OBL_OF_SURVIVOR, OBL_MOVED_2)]


def test_the_digest_binds_the_listed_pairs(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    _set_text(rig, OBL_OF_SURVIVOR, _TEXT)
    without = role_plan(rig).state_digest

    _set_text(rig, OBL_MOVED_1, _TEXT)

    assert role_plan(rig).state_digest != without


@pytest.mark.parametrize("moved", [OBL_MOVED_1, OBL_MOVED_2])
def test_after_the_merge_find_duplicate_obligations_returns_the_listed_pair_as_a_group(
    cleanup_rig: CleanupRig, moved: str
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    _set_text(rig, OBL_OF_SURVIVOR, _TEXT)
    _set_text(rig, moved, _TEXT)
    [pair] = role_plan(rig).preview.duplicate_obligation_pairs

    run_role_merge(rig)
    result = find_duplicate_obligations(rig.read, role_id=ROLE_SURVIVOR)

    [group] = result.groups
    assert group.role_id == ROLE_SURVIVOR
    assert {m.id for m in group.members} == {
        pair.survivor_obligation_id,
        pair.absorbed_obligation_id,
    }
