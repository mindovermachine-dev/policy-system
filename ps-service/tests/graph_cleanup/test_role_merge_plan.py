"""Pure planning and validation for `merge-roles`: no I/O, the read state in, the plan out.

The preview names both Roles, the one defining instrument and the Obligations that move. The
state digest binds an approval to the exact state previewed; validation rejects every pair that
must never get an approval.
"""

from __future__ import annotations

import pytest

from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.models import (
    EdgeRecord,
    RoleDefines,
    RoleMergeState,
    RoleState,
)
from ps_service.graph_cleanup.role_planner import plan_role_merge, validate_role_merge

_INSTRUMENT = "reg_cra"
_SURVIVOR = "role_survivor"
_ABSORBED = "role_absorbed"


def _state(**overrides: object) -> RoleMergeState:
    fields: dict[str, object] = {
        "survivor_id": _SURVIVOR,
        "absorbed_id": _ABSORBED,
        "survivor": RoleState(
            id=_SURVIVOR,
            name="Market Surveillance Authority",
            properties={"description": "The authority", "confidence": 0.9},
        ),
        "absorbed": RoleState(
            id=_ABSORBED, name="market surveillance authorities", properties={"confidence": 0.8}
        ),
        "survivor_defines": (RoleDefines(instrument_id=_INSTRUMENT, source_ref="Art. 3(1)"),),
        "absorbed_defines": (RoleDefines(instrument_id=_INSTRUMENT, source_ref="Art. 3(2)"),),
        "survivor_obligation_ids": ("obl_kept",),
        "absorbed_obligation_ids": ("obl_b", "obl_a"),
        "absorbed_degree": 3,
        **overrides,
    }
    return RoleMergeState.model_validate(fields)


def test_the_preview_names_both_roles_the_instrument_and_the_obligations_that_move() -> None:
    plan = plan_role_merge(_state())

    preview = plan.preview
    assert preview.survivor_id == _SURVIVOR
    assert preview.survivor_name == "Market Surveillance Authority"
    assert preview.absorbed_id == _ABSORBED
    assert preview.absorbed_name == "market surveillance authorities"
    assert preview.instrument_id == _INSTRUMENT
    assert preview.moved_obligation_ids == ("obl_a", "obl_b")
    assert preview.state_digest == plan.state_digest
    assert len(plan.state_digest) == 64


def test_the_after_snapshot_keeps_the_survivor_and_marks_the_absorbed_role() -> None:
    plan = plan_role_merge(_state())

    assert {(n.label, n.id) for n in plan.before.nodes} == {
        ("Role", _SURVIVOR),
        ("Role", _ABSORBED),
    }
    marker = next(n for n in plan.after.nodes if n.label == "MergedRole")
    assert (marker.id, marker.properties) == (_ABSORBED, {"merged_into": _SURVIVOR})
    assert all(n.id != _ABSORBED for n in plan.after.nodes if n.label == "Role")
    has_after = {(e.source_id, e.target_id) for e in plan.after.edges if e.rel_type == "HAS"}
    assert has_after == {(_SURVIVOR, "obl_a"), (_SURVIVOR, "obl_b")}
    has_before = {(e.source_id, e.target_id) for e in plan.before.edges if e.rel_type == "HAS"}
    assert has_before == {(_ABSORBED, "obl_a"), (_ABSORBED, "obl_b")}
    assert (
        EdgeRecord(
            rel_type="DEFINES",
            source_label="RegulatoryInstrument",
            source_id=_INSTRUMENT,
            target_label="Role",
            target_id=_ABSORBED,
        )
        in plan.before.edges
    )


def test_the_plan_pins_what_the_write_must_find_and_what_the_survivor_ends_with() -> None:
    plan = plan_role_merge(_state())

    assert plan.expected.absorbed_obligation_ids == ("obl_a", "obl_b")
    assert plan.expected.survivor_obligation_ids == ("obl_kept",)
    assert plan.survivor_has_count_after_merge == 3
    assert plan.absorbed_defines_source_ref == "Art. 3(2)"


def test_the_digest_is_stable_and_ignores_the_order_the_reads_returned() -> None:
    reordered = _state(absorbed_obligation_ids=("obl_a", "obl_b"))

    assert plan_role_merge(_state()).state_digest == plan_role_merge(reordered).state_digest


@pytest.mark.parametrize(
    "change",
    [
        {"survivor_obligation_ids": ("obl_kept", "obl_new")},
        {"absorbed_obligation_ids": ("obl_a",), "absorbed_degree": 2},
        {"absorbed_defines": (RoleDefines(instrument_id=_INSTRUMENT, source_ref="Art. 9"),)},
        {"survivor_defines": (RoleDefines(instrument_id=_INSTRUMENT, source_ref="Art. 9"),)},
    ],
    ids=["survivor-has", "absorbed-has", "absorbed-defines", "survivor-defines"],
)
def test_the_digest_changes_when_either_roles_has_or_defines_edges_change(
    change: dict[str, object],
) -> None:
    changed = _state(**change)

    assert plan_role_merge(_state()).state_digest != plan_role_merge(changed).state_digest


def test_a_valid_pair_validates_to_the_two_roles_and_their_instrument() -> None:
    survivor, absorbed, instrument_id = validate_role_merge(_state())

    assert (survivor.id, absorbed.id, instrument_id) == (_SURVIVOR, _ABSORBED, _INSTRUMENT)


def test_a_role_with_no_obligations_merges_with_nothing_to_move() -> None:
    plan = plan_role_merge(_state(absorbed_obligation_ids=(), absorbed_degree=1))

    assert plan.preview.moved_obligation_ids == ()
    assert plan.survivor_has_count_after_merge == 1


def _message(state: RoleMergeState) -> str:
    with pytest.raises(GraphCleanupValidationError) as raised:
        validate_role_merge(state)
    return str(raised.value)


def test_a_role_cannot_be_merged_into_itself() -> None:
    state = _state(absorbed_id=_SURVIVOR, absorbed=_state().survivor)

    assert "into itself" in _message(state)


def test_the_same_missing_id_twice_is_a_self_merge_not_a_missing_role() -> None:
    state = _state(survivor_id="role_x", absorbed_id="role_x", survivor=None, absorbed=None)

    assert "into itself" in _message(state)


@pytest.mark.parametrize("side", ["survivor", "absorbed"])
def test_a_side_that_is_not_a_role_does_not_exist(side: str) -> None:
    message = _message(_state(**{side: None}))

    assert f"the {side} role does not exist" in message


@pytest.mark.parametrize("side", ["survivor", "absorbed"])
def test_a_side_whose_id_is_a_merged_role_marker_is_named_as_one(side: str) -> None:
    marker_id = _SURVIVOR if side == "survivor" else _ABSORBED

    message = _message(_state(**{side: None, "marker_ids": (marker_id,)}))

    assert f"the {side} id {marker_id!r} is a MergedRole marker" in message


@pytest.mark.parametrize("side", ["survivor", "absorbed"])
@pytest.mark.parametrize(
    "defines",
    [
        (),
        (
            RoleDefines(instrument_id=_INSTRUMENT, source_ref="Art. 1"),
            RoleDefines(instrument_id="reg_other", source_ref="Art. 2"),
        ),
    ],
    ids=["no-defines", "two-defines"],
)
def test_a_role_not_defined_by_exactly_one_instrument_is_rejected(
    side: str, defines: tuple[RoleDefines, ...]
) -> None:
    message = _message(_state(**{f"{side}_defines": defines}))

    assert f"the {side} role" in message
    assert "exactly one instrument" in message


def test_roles_defined_by_different_instruments_are_rejected_naming_both() -> None:
    other = (RoleDefines(instrument_id="reg_gdpr", source_ref="Art. 4"),)

    message = _message(_state(absorbed_defines=other))

    assert "different instruments" in message
    assert _INSTRUMENT in message
    assert "reg_gdpr" in message


def test_an_absorbed_role_with_an_edge_that_is_neither_defines_nor_has_is_rejected() -> None:
    message = _message(_state(absorbed_degree=4))

    assert "other than DEFINES and HAS" in message


def test_a_moved_obligation_also_borne_by_a_third_role_is_rejected() -> None:
    third = EdgeRecord(
        rel_type="HAS",
        source_label="Role",
        source_id="role_third",
        target_label="Obligation",
        target_id="obl_a",
    )

    message = _message(_state(foreign_bearers=(third,)))

    assert "obl_a" in message
    assert "role_third" in message
    assert "borne by the absorbed role alone" in message


def test_a_moved_obligation_the_survivor_also_bears_is_rejected() -> None:
    both = EdgeRecord(
        rel_type="HAS",
        source_label="Role",
        source_id=_SURVIVOR,
        target_label="Obligation",
        target_id="obl_a",
    )

    message = _message(_state(foreign_bearers=(both,)))

    assert _SURVIVOR in message
    assert "borne by the absorbed role alone" in message


def test_a_state_without_obligation_texts_lists_no_duplicate_pairs() -> None:
    assert plan_role_merge(_state()).preview.duplicate_obligation_pairs == ()
