"""Case 3 on the preview/approval path (issue #190, slice 13 d; AC-BI-011, AC-BI-012, D6).

Different policies: rejected BEFORE any approval row exists, naming both policies and the
release-governance step. Same policy: no acknowledgment, an approval is created at once.
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
from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.service import (
    create_capability_merge_approval,
    preview_capability_merge,
)
from ps_service.logging import configure

_ACTOR = ("officer", "https://issuer.example.com/")


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _same_policy_graph(status: str = "approved") -> ScriptedMergeGraph:
    return ScriptedMergeGraph(
        governors=[
            [SURVIVOR, "pol_1", "Incident Policy", status],
            [ABSORBED, "pol_1", "Incident Policy", status],
        ],
        governed_sets=[["pol_1", SURVIVOR], ["pol_1", ABSORBED], ["pol_1", "cap_other"]],
    )


def _different_policy_graph(
    survivor_status: str = "approved", absorbed_status: str = "approved"
) -> ScriptedMergeGraph:
    return ScriptedMergeGraph(
        governors=[
            [SURVIVOR, "pol_s", "Survivor Policy", survivor_status],
            [ABSORBED, "pol_a", "Absorbed Policy", absorbed_status],
        ]
    )


def _create(graph: ScriptedMergeGraph, store: FakeApprovalStore, *, acknowledge: bool = False):
    return create_capability_merge_approval(
        graph,
        survivor_id=SURVIVOR,
        absorbed_id=ABSORBED,
        acknowledge_governance_change=acknowledge,
        actor=_ACTOR,
        base_url="https://ps.example.com",
        store=store,
    )


@pytest.mark.parametrize("acknowledge", [False, True])
def test_different_policies_are_rejected_before_any_approval_even_when_acknowledged(
    *, acknowledge: bool
) -> None:
    store = FakeApprovalStore()
    graph = _different_policy_graph()

    with pytest.raises(GraphCleanupValidationError) as excinfo:
        _create(graph, store, acknowledge=acknowledge)

    text = str(excinfo.value)
    for fragment in ("pol_s", "Survivor Policy", "pol_a", "Absorbed Policy"):
        assert fragment in text
    assert "release-capability-governance" in text
    assert approval_row_count(store) == 0
    assert graph.write_calls == []


def test_the_preview_of_a_different_policy_pair_is_the_same_rejection() -> None:
    with pytest.raises(GraphCleanupValidationError, match="release-capability-governance"):
        preview_capability_merge(
            _different_policy_graph(absorbed_status="draft"),
            survivor_id=SURVIVOR,
            absorbed_id=ABSORBED,
        )


def test_the_same_policy_needs_no_acknowledgment_and_creates_an_approval() -> None:
    store = FakeApprovalStore()

    approval = _create(_same_policy_graph(), store, acknowledge=False)

    assert approval.preview.policy_case == 3
    assert approval_row_count(store) == 1
    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    assert row.normalized_args["acknowledge_governance_change"] is False


def test_the_same_policy_preview_names_the_policy_and_states_no_acknowledgment_is_needed() -> None:
    preview = preview_capability_merge(
        _same_policy_graph("draft"), survivor_id=SURVIVOR, absorbed_id=ABSORBED
    )

    assert preview.governance is not None
    assert preview.governance.policy.id == "pol_1"
    assert preview.governance.policy.status == "draft"
    assert preview.governance.acknowledgment_required is False
    assert preview.governance.governed_set_after == ("cap_other", SURVIVOR)


def test_acknowledging_a_same_policy_pair_is_harmless_and_signed_verbatim() -> None:
    store = FakeApprovalStore()

    approval = _create(_same_policy_graph(), store, acknowledge=True)

    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    assert row.normalized_args["acknowledge_governance_change"] is True
