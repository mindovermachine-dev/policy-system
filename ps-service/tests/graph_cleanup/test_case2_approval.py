"""Case-2 acknowledgment gate on the approval (issue #190, slice 12 d; AC-BI-010, D5).

With exactly one capability governed, a first call without `acknowledge_governance_change`
returns the preview and creates NO approval; the acknowledged call creates the approval with
the flag inside the signed `normalized_args`. Case 1 is unaffected.
"""

from __future__ import annotations

import pytest

from graph_cleanup._fakes import (
    ABSORBED,
    SURVIVOR,
    FakeApprovalStore,
    ScriptedMergeGraph,
    approval_row_count,
)
from ps_service.graph_cleanup.errors import GraphCleanupAcknowledgmentRequiredError
from ps_service.graph_cleanup.service import (
    create_capability_merge_approval,
    preview_capability_merge,
)
from ps_service.logging import configure

_ACTOR = ("officer", "https://issuer.example.com/")


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _case2_graph(status: str = "approved") -> ScriptedMergeGraph:
    return ScriptedMergeGraph(governors=[[ABSORBED, "pol_1", "Incident Policy", status]])


def _create(graph: ScriptedMergeGraph, store: FakeApprovalStore, *, acknowledge: bool):
    return create_capability_merge_approval(
        graph,
        survivor_id=SURVIVOR,
        absorbed_id=ABSORBED,
        acknowledge_governance_change=acknowledge,
        actor=_ACTOR,
        base_url="https://ps.example.com",
        store=store,
    )


def test_preview_of_a_case_two_pair_names_policy_status_and_coverage_count() -> None:
    preview = preview_capability_merge(_case2_graph(), survivor_id=SURVIVOR, absorbed_id=ABSORBED)

    assert preview.policy_case == 2
    assert preview.governance is not None
    assert (preview.governance.policy.id, preview.governance.policy.status) == (
        "pol_1",
        "approved",
    )
    assert preview.governance.policy.title == "Incident Policy"
    # survivor (ungoverned) is required by no obligation in the default fake: nothing gains cover
    assert preview.governance.obligations_coverage_changed == 0


def test_without_the_acknowledgment_no_approval_is_created_and_the_preview_is_carried() -> None:
    graph = _case2_graph()
    store = FakeApprovalStore()

    with pytest.raises(GraphCleanupAcknowledgmentRequiredError) as excinfo:
        _create(graph, store, acknowledge=False)

    assert approval_row_count(store) == 0
    assert graph.write_calls == []
    assert excinfo.value.preview.policy_case == 2
    assert excinfo.value.preview.governance is not None
    assert "`approved`" in str(excinfo.value)
    assert "acknowledge_governance_change" in str(excinfo.value)


def test_with_the_acknowledgment_the_flag_is_inside_the_signed_arguments() -> None:
    graph = _case2_graph()
    store = FakeApprovalStore()

    approval = _create(graph, store, acknowledge=True)

    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    assert row.normalized_args == {
        "survivor_id": SURVIVOR,
        "absorbed_id": ABSORBED,
        "acknowledge_governance_change": True,
        "state_digest": approval.preview.state_digest,
    }
    assert row.display_summary["policy_case"] == 2
    governance = row.display_summary["governance"]
    assert isinstance(governance, dict)
    assert governance["policy"] == {
        "id": "pol_1",
        "title": "Incident Policy",
        "status": "approved",
    }
    assert graph.write_calls == []


def test_the_acknowledged_and_unacknowledged_arguments_sign_differently() -> None:
    from ps_service.passkey_signing.service import (
        _compute_sign_challenge,  # pyright: ignore[reportPrivateUsage]
    )

    graph = _case2_graph()
    store = FakeApprovalStore()
    approval = _create(graph, store, acknowledge=True)
    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    unacknowledged = {**row.normalized_args, "acknowledge_governance_change": False}
    from dataclasses import replace

    assert _compute_sign_challenge(row) != _compute_sign_challenge(
        replace(row, normalized_args=unacknowledged)
    )


def test_case_one_needs_no_acknowledgment() -> None:
    graph = ScriptedMergeGraph()
    store = FakeApprovalStore()

    approval = _create(graph, store, acknowledge=False)

    assert approval.preview.policy_case == 1
    assert approval.preview.governance is None
    assert approval_row_count(store) == 1


def test_a_governed_survivor_is_also_case_two_and_needs_the_acknowledgment() -> None:
    graph = ScriptedMergeGraph(governors=[[SURVIVOR, "pol_1", "Incident Policy", "draft"]])
    store = FakeApprovalStore()

    with pytest.raises(GraphCleanupAcknowledgmentRequiredError) as excinfo:
        _create(graph, store, acknowledge=False)

    assert excinfo.value.preview.governance is not None
    assert excinfo.value.preview.governance.governed_side == "survivor"
    assert approval_row_count(store) == 0
