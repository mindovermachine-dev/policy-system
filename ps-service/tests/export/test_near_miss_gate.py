"""Tests for `ps_service.export.near_miss_gate`.

The gate reads the instrument's `{short}_baseline` Capability ids and the resolvable
`PendingReview`s of `policy_system` (through `list_pending_reviews`), and returns the ids of the
reviews that touch one of those Capabilities. The baseline side is a real rig graph behind
`ExportReadGraph`; the `policy_system` side is a `ScriptedPolicyGraph`, because fakes cannot run
the multi-clause review query.
"""

from __future__ import annotations

from graph_gateway._fakes import GatewayRig

from export._gateway_fakes import ExportRig, ScriptedPolicyGraph, pending_review
from ps_service.company_merge.pending_review import list_pending_reviews
from ps_service.export.near_miss_gate import unresolved_near_miss_review_ids


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
