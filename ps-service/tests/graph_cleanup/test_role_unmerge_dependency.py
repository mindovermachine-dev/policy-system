"""A Role merge that later merges depend on cannot be unmerged.

A dependent is found only through the audit trail, which is authoritative: a later `applied`
`obligation.merge` that moved or absorbed one of the Obligations the Role merge moved, or a later
`role.merge` that absorbed the Role merge's survivor. A dependent that never took effect (a `failed`
row for its approval) or was itself reversed does not block. The refusal names the dependent's
audit event id and approval id and creates no approval. A graph change with no audit row is not a
dependent: the live checks of the plan refuse it as stale.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import pytest

from graph_cleanup._fakes import (
    OBL_MOVED_1,
    OBL_MOVED_2,
    OBL_OF_SURVIVOR,
    ROLE_ABSORBED,
    ROLE_SURVIVOR,
)
from graph_cleanup._unmerge_runs import role_unmerge_world, unmerge_plan
from ps_service.audit.models import AuditEventRow
from ps_service.graph_cleanup.audit_actions import (
    ObligationMergeDetails,
    ObligationUnmergeDetails,
    RoleMergeDetails,
    RoleUnmergeDetails,
)
from ps_service.graph_cleanup.errors import GraphCleanupValidationError

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig

_BASE = datetime(2026, 10, 5, 1, 0, tzinfo=UTC)
"""After the Role merge row (hour 0 of the same day), so every row built here is later."""

_Outcome = Literal["applied", "failed"]


def _row(
    n: int, action: str, details: dict[str, object], *, outcome: _Outcome = "applied"
) -> AuditEventRow:
    return AuditEventRow(
        id=f"evt-{n}",
        occurred_at=_BASE + timedelta(minutes=n),
        actor_subject="officer",
        actor_issuer="https://issuer.example.com/",
        action=action,
        resource_type=action.split(".", maxsplit=1)[0],
        resource_id=str(details["absorbed_id"]),
        outcome=outcome,
        details=details,
    )


def _obligation_merge(
    n: int, survivor: str, absorbed: str, *, outcome: _Outcome = "applied"
) -> AuditEventRow:
    details = ObligationMergeDetails(
        survivor_id=survivor, absorbed_id=absorbed, role_id=ROLE_SURVIVOR, approval_id=f"ap-{n}"
    ).model_dump(mode="json")
    return _row(n, "obligation.merge", details, outcome=outcome)


def _role_merge(n: int, survivor: str, absorbed: str) -> AuditEventRow:
    details = RoleMergeDetails(
        survivor_id=survivor, absorbed_id=absorbed, approval_id=f"ap-{n}", instrument_id="reg_cra"
    ).model_dump(mode="json")
    return _row(n, "role.merge", details)


def _obligation_unmerge(
    n: int, survivor: str, absorbed: str, reverses: str, *, outcome: _Outcome = "applied"
) -> AuditEventRow:
    details = ObligationUnmergeDetails(
        survivor_id=survivor,
        absorbed_id=absorbed,
        role_id=ROLE_SURVIVOR,
        approval_id=f"ap-{n}",
        reverses_approval_id=reverses,
    ).model_dump(mode="json")
    return _row(n, "obligation.unmerge", details, outcome=outcome)


def _role_unmerge(n: int, survivor: str, absorbed: str, reverses: str) -> AuditEventRow:
    details = RoleUnmergeDetails(
        survivor_id=survivor,
        absorbed_id=absorbed,
        approval_id=f"ap-{n}",
        reverses_approval_id=reverses,
    ).model_dump(mode="json")
    return _row(n, "role.unmerge", details)


def _world(rig: CleanupRig, *later: AuditEventRow) -> None:
    """The Role merge's aftermath with `later` rows (given oldest first) after its row."""
    role_unmerge_world(rig)
    rig.audit.history = [*reversed(later), *rig.audit.history]


def _refusal(rig: CleanupRig) -> str:
    with pytest.raises(GraphCleanupValidationError) as raised:
        unmerge_plan(rig, ROLE_ABSORBED)
    return str(raised.value)


def test_without_a_later_merge_the_unmerge_is_planned(cleanup_rig: CleanupRig) -> None:
    _world(cleanup_rig)

    assert unmerge_plan(cleanup_rig, ROLE_ABSORBED).preview.kind == "role"


def test_a_later_obligation_merge_that_absorbed_a_moved_obligation_blocks_the_unmerge(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig, _obligation_merge(3, "obl_other", OBL_MOVED_1))

    message = _refusal(cleanup_rig)

    assert "evt-3" in message
    assert "ap-3" in message
    assert "obligation.merge" in message


def test_a_later_obligation_merge_whose_survivor_is_a_moved_obligation_blocks_the_unmerge(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig, _obligation_merge(4, OBL_MOVED_2, "obl_other"))

    message = _refusal(cleanup_rig)

    assert "evt-4" in message
    assert "ap-4" in message


def test_a_later_role_merge_that_absorbed_the_survivor_blocks_the_unmerge(
    cleanup_rig: CleanupRig,
) -> None:
    _world(cleanup_rig, _role_merge(5, "role_bigger", ROLE_SURVIVOR))

    message = _refusal(cleanup_rig)

    assert "evt-5" in message
    assert "ap-5" in message
    assert "role.merge" in message


def test_a_dependency_is_refused_before_the_live_graph_is_judged(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    _world(rig, _role_merge(5, "role_bigger", ROLE_SURVIVOR))
    del rig.graph.nodes[("Role", ROLE_SURVIVOR)]  # the live state would refuse too, differently

    assert "evt-5" in _refusal(rig)


def test_the_newest_dependent_is_the_one_named(cleanup_rig: CleanupRig) -> None:
    _world(
        cleanup_rig,
        _obligation_merge(3, "obl_other", OBL_MOVED_1),
        _obligation_merge(6, "obl_other", OBL_MOVED_2),
    )

    assert "evt-6" in _refusal(cleanup_rig)


@pytest.mark.parametrize(
    "later",
    [
        _obligation_merge(3, "obl_a", "obl_b"),
        _obligation_merge(3, OBL_OF_SURVIVOR, "obl_b"),
        _role_merge(3, ROLE_SURVIVOR, "role_small"),
        _role_merge(3, "role_x", "role_y"),
    ],
    ids=[
        "obligation_merge_of_others",
        "survivors_own_obligation",
        "survivor_absorbs_another_role",
        "role_merge_of_others",
    ],
)
def test_a_later_merge_that_does_not_involve_the_moved_set_or_the_survivor_does_not_block(
    cleanup_rig: CleanupRig, later: AuditEventRow
) -> None:
    _world(cleanup_rig, later)

    assert unmerge_plan(cleanup_rig, ROLE_ABSORBED).preview.kind == "role"


def test_a_dependent_that_never_took_effect_does_not_block(cleanup_rig: CleanupRig) -> None:
    _world(
        cleanup_rig,
        _obligation_merge(3, "obl_other", OBL_MOVED_1),
        _obligation_merge(4, "obl_other", OBL_MOVED_1, outcome="failed"),
    )
    cleanup_rig.audit.history = [
        replace(e, details={**e.details, "approval_id": "ap-3"}) if e.id == "evt-4" else e
        for e in cleanup_rig.audit.history
    ]

    assert unmerge_plan(cleanup_rig, ROLE_ABSORBED).preview.kind == "role"


@pytest.mark.parametrize(
    "reversal",
    [
        _obligation_unmerge(4, "obl_other", OBL_MOVED_1, "ap-3"),
        _role_unmerge(4, "obl_other", OBL_MOVED_1, "ap-3"),
    ],
    ids=["reversed_by_obligation_unmerge", "reversed_by_role_unmerge_row"],
)
def test_a_dependent_that_was_itself_reversed_does_not_block(
    cleanup_rig: CleanupRig, reversal: AuditEventRow
) -> None:
    _world(cleanup_rig, _obligation_merge(3, "obl_other", OBL_MOVED_1), reversal)

    assert unmerge_plan(cleanup_rig, ROLE_ABSORBED).preview.kind == "role"


def test_a_reversal_that_failed_does_not_lift_the_block(cleanup_rig: CleanupRig) -> None:
    _world(
        cleanup_rig,
        _obligation_merge(3, "obl_other", OBL_MOVED_1),
        _obligation_unmerge(4, "obl_other", OBL_MOVED_1, "ap-3", outcome="failed"),
    )

    assert "evt-3" in _refusal(cleanup_rig)


def test_a_merge_from_before_the_role_merge_is_not_a_dependent(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    role_unmerge_world(rig)
    earlier = replace(
        _obligation_merge(1, "obl_other", OBL_MOVED_1), occurred_at=_BASE - timedelta(days=1)
    )
    rig.audit.history = [*rig.audit.history, earlier]

    assert unmerge_plan(rig, ROLE_ABSORBED).preview.kind == "role"


def test_a_dependent_beyond_the_first_page_of_the_trail_is_still_found(
    cleanup_rig: CleanupRig,
) -> None:
    unrelated = [_obligation_merge(100 + i, f"obl_s{i}", f"obl_a{i}") for i in range(130)]
    _world(cleanup_rig, _obligation_merge(3, "obl_other", OBL_MOVED_1), *unrelated)

    assert "evt-3" in _refusal(cleanup_rig)


# -- F3: no graph-only backstop; unaudited drift is refused by the live checks ---------------------


def test_an_unaudited_merge_of_a_moved_obligation_is_refused_by_the_live_checks(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    del rig.graph.nodes[("Obligation", OBL_MOVED_1)]
    for key in [k for k in rig.graph.edges if OBL_MOVED_1 in (k[2], k[4])]:
        del rig.graph.edges[key]
    rig.graph.nodes[("MergedObligation", OBL_MOVED_1)] = {"merged_into": "obl_other"}

    message = _refusal(rig)

    assert OBL_MOVED_1 in message
    assert "evt-" not in message
