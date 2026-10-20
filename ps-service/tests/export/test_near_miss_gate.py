"""Tests for `ps_service.export.near_miss_gate`.

The gate reads the instrument's `{short}_baseline` node ids of every registered reviewable kind
and the resolvable `PendingReview`s of `policy_system` (through `list_pending_reviews`), and
returns the ids of the reviews that touch a node of their own kind. The baseline side is a real
rig graph behind `ExportReadGraph`; the `policy_system` side is a `ScriptedPolicyGraph`, because
fakes cannot run the multi-clause review query.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from company_merge._kinds import UNREGISTERED_KIND
from graph_gateway._fakes import GatewayRig

from export._gateway_fakes import ExportRig, ScriptedPolicyGraph, pending_review
from ps_service.company_merge.errors import UnknownReviewKindError
from ps_service.company_merge.pending_review import list_pending_reviews
from ps_service.export.near_miss_gate import unresolved_near_miss_review_ids

if TYPE_CHECKING:
    from graph_gateway._fakes import FakeQueryResult

    from ps_service.company_merge.reviewable_kinds import ReviewableKind


def _baseline_with_capabilities(*capability_ids: str) -> ExportRig:
    export_rig = ExportRig(GatewayRig())
    for capability_id in capability_ids:
        ExportRig.seed_node(export_rig.baseline, "Capability", capability_id, {"text": "x"})
    return export_rig


def test_review_on_the_incoming_side_is_returned() -> None:
    export_rig = _baseline_with_capabilities("cap_a", "cap_b")
    policy = ScriptedPolicyGraph([pending_review("review_1", incoming_id="cap_a")])

    ids = unresolved_near_miss_review_ids(export_rig.baseline_reader(), policy)

    assert ids == ("review_1",)


def test_review_on_the_existing_side_is_returned() -> None:
    export_rig = _baseline_with_capabilities("cap_a", "cap_b")
    policy = ScriptedPolicyGraph([pending_review("review_2", nearest_existing_id="cap_b")])

    ids = unresolved_near_miss_review_ids(export_rig.baseline_reader(), policy)

    assert ids == ("review_2",)


def test_review_matching_on_both_sides_is_counted_once_and_ids_are_sorted() -> None:
    export_rig = _baseline_with_capabilities("cap_a", "cap_b")
    policy = ScriptedPolicyGraph(
        [
            pending_review("review_z", incoming_id="cap_a", nearest_existing_id="cap_b"),
            pending_review("review_a", incoming_id="cap_b"),
        ]
    )

    ids = unresolved_near_miss_review_ids(export_rig.baseline_reader(), policy)

    assert ids == ("review_a", "review_z")


def test_review_naming_a_capability_of_another_instrument_is_not_returned() -> None:
    export_rig = _baseline_with_capabilities("cap_a")
    policy = ScriptedPolicyGraph(
        [pending_review("review_other", incoming_id="cap_x", nearest_existing_id="cap_y")]
    )

    ids = unresolved_near_miss_review_ids(export_rig.baseline_reader(), policy)

    assert ids == ()


def test_review_with_one_side_in_the_instrument_and_one_foreign_side_is_returned() -> None:
    export_rig = _baseline_with_capabilities("cap_a")
    policy = ScriptedPolicyGraph(
        [pending_review("review_mixed", incoming_id="cap_x", nearest_existing_id="cap_a")]
    )

    ids = unresolved_near_miss_review_ids(export_rig.baseline_reader(), policy)

    assert ids == ("review_mixed",)


def test_no_reviews_yields_no_ids() -> None:
    export_rig = _baseline_with_capabilities("cap_a")

    ids = unresolved_near_miss_review_ids(export_rig.baseline_reader(), ScriptedPolicyGraph())

    assert ids == ()


def test_scripted_policy_graph_rows_round_trip_through_list_pending_reviews() -> None:
    """The scripted handle's row layout is the one `list_pending_reviews` reads."""
    record = pending_review("review_rt", incoming_id="cap_a", nearest_existing_id="cap_b")

    assert list_pending_reviews(ScriptedPolicyGraph([record])) == (record,)


def test_gate_sends_the_same_single_statement_as_list_pending_reviews() -> None:
    """Reuse proof, not predicate proof: the gate adds no review query of its own.

    The "resolvable" predicate (both nodes exist under the kind, neither `merged`) lives in the
    one statement `list_pending_reviews` sends; fakes cannot execute it, so the predicate itself
    is proven by `test_near_miss_gate_live.py`.
    """
    export_rig = _baseline_with_capabilities("cap_a")
    gate_policy = ScriptedPolicyGraph()
    reference_policy = ScriptedPolicyGraph()

    unresolved_near_miss_review_ids(export_rig.baseline_reader(), gate_policy)
    list_pending_reviews(reference_policy)

    assert len(gate_policy.statements) == 1
    assert gate_policy.statements == reference_policy.statements


def test_review_of_a_registered_non_capability_kind_blocks_export(
    test_only_reviewable_kind: ReviewableKind,
) -> None:
    label = test_only_reviewable_kind.label
    export_rig = _baseline_with_capabilities("cap_a")
    ExportRig.seed_node(export_rig.baseline, label, "probe_a", {"name": "x"})
    policy = ScriptedPolicyGraph([pending_review("review_p", kind=label, incoming_id="probe_a")])

    ids = unresolved_near_miss_review_ids(export_rig.baseline_reader(), policy)

    assert ids == ("review_p",)


def test_review_only_matches_ids_of_its_own_kind(test_only_reviewable_kind: ReviewableKind) -> None:
    label = test_only_reviewable_kind.label
    export_rig = _baseline_with_capabilities("shared_id")
    ExportRig.seed_node(export_rig.baseline, label, "probe_a", {"name": "x"})
    policy = ScriptedPolicyGraph(
        [
            pending_review("review_cap_names_probe_id", kind="Capability", incoming_id="probe_a"),
            pending_review("review_probe_names_cap_id", kind=label, incoming_id="shared_id"),
        ]
    )

    ids = unresolved_near_miss_review_ids(export_rig.baseline_reader(), policy)

    assert ids == ()


def _baseline_with_roles(*role_ids: str) -> ExportRig:
    export_rig = ExportRig(GatewayRig())
    for role_id in role_ids:
        ExportRig.seed_node(export_rig.baseline, "Role", role_id, {"name": "Operator"})
    return export_rig


def test_an_unresolved_role_review_on_the_incoming_side_is_returned() -> None:
    export_rig = _baseline_with_roles("role_a", "role_b")
    policy = ScriptedPolicyGraph([pending_review("review_role", kind="Role", incoming_id="role_a")])

    ids = unresolved_near_miss_review_ids(export_rig.baseline_reader(), policy)

    assert ids == ("review_role",)


def test_an_unresolved_role_review_on_the_existing_side_is_returned() -> None:
    export_rig = _baseline_with_roles("role_a", "role_b")
    policy = ScriptedPolicyGraph(
        [pending_review("review_role", kind="Role", nearest_existing_id="role_b")]
    )

    ids = unresolved_near_miss_review_ids(export_rig.baseline_reader(), policy)

    assert ids == ("review_role",)


def test_a_role_review_about_another_instruments_roles_does_not_block() -> None:
    export_rig = _baseline_with_roles("role_a")
    policy = ScriptedPolicyGraph(
        [
            pending_review(
                "review_other",
                kind="Role",
                incoming_id="role_x",
                nearest_existing_id="role_y",
            )
        ]
    )

    ids = unresolved_near_miss_review_ids(export_rig.baseline_reader(), policy)

    assert ids == ()


def test_a_capability_id_equal_to_a_role_id_does_not_cross_match() -> None:
    export_rig = _baseline_with_roles("shared_id")
    ExportRig.seed_node(export_rig.baseline, "Capability", "cap_a", {"name": "x"})
    policy = ScriptedPolicyGraph(
        [
            pending_review("review_cap_names_role_id", kind="Capability", incoming_id="shared_id"),
            pending_review("review_role_names_cap_id", kind="Role", incoming_id="cap_a"),
        ]
    )

    ids = unresolved_near_miss_review_ids(export_rig.baseline_reader(), policy)

    assert ids == ()


def test_baseline_ids_are_read_once_per_registered_kind(
    test_only_reviewable_kind: ReviewableKind,
) -> None:
    export_rig = _baseline_with_capabilities("cap_a")
    reader = export_rig.baseline_reader()

    unresolved_near_miss_review_ids(reader, ScriptedPolicyGraph())

    assert reader.statements == [
        "MATCH (n:Capability) RETURN n.id",
        "MATCH (n:Role) RETURN n.id",
        f"MATCH (n:{test_only_reviewable_kind.label}) RETURN n.id",
    ]


def test_gate_rejects_a_review_whose_kind_has_no_spec_naming_the_kind() -> None:
    export_rig = _baseline_with_capabilities("cap_a")
    policy = ScriptedPolicyGraph([pending_review("review_stray", kind=UNREGISTERED_KIND)])

    with pytest.raises(UnknownReviewKindError, match=UNREGISTERED_KIND):
        unresolved_near_miss_review_ids(export_rig.baseline_reader(), policy)


class _BaselineFailingFor:
    """A baseline handle whose id read fails for one label only; the rest is the rig's."""

    def __init__(self, inner: object, label: str, error: Exception) -> None:
        self._inner = inner
        self._failing_statement = f"MATCH (n:{label}) RETURN n.id"
        self._error = error

    def query(self, q: str, params: dict[str, object] | None = None) -> FakeQueryResult:
        if q == self._failing_statement:
            raise self._error
        return self._inner.query(q, params)  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownVariableType]


@pytest.mark.parametrize("failing", ["Capability", "ProbeItem"])
def test_baseline_read_failure_propagates_for_every_registered_kind(
    failing: str, test_only_reviewable_kind: ReviewableKind
) -> None:
    assert test_only_reviewable_kind.label == "ProbeItem"
    export_rig = _baseline_with_capabilities("cap_a")
    baseline = _BaselineFailingFor(
        export_rig.baseline_reader(), failing, ConnectionError("baseline down")
    )

    with pytest.raises(ConnectionError, match="baseline down"):
        unresolved_near_miss_review_ids(baseline, ScriptedPolicyGraph())  # pyright: ignore[reportArgumentType]


def test_policy_system_read_failure_propagates() -> None:
    export_rig = _baseline_with_capabilities("cap_a")
    policy = ScriptedPolicyGraph(fail_with=ConnectionError("policy_system down"))

    with pytest.raises(ConnectionError, match="policy_system down"):
        unresolved_near_miss_review_ids(export_rig.baseline_reader(), policy)
