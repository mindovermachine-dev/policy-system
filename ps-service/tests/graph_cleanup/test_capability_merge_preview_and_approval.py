"""Preview and pair-bound approval for `merge-capabilities` (issue #190, slice 10 sub-step d).

AC-BI-005 (preview with no writes + approval bound to the exact pair), AC-BI-016 (rejections
create no approval), AC-BI-017 (a swapped pair yields a different signing challenge).
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
from ps_service.passkey_signing.service import (
    _compute_sign_challenge,  # pyright: ignore[reportPrivateUsage]
)

_ACTOR = ("officer", "https://issuer.example.com/")


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _create(
    graph: ScriptedMergeGraph,
    store: FakeApprovalStore,
    *,
    survivor_id: str = SURVIVOR,
    absorbed_id: str = ABSORBED,
    acknowledge: bool = False,
):
    return create_capability_merge_approval(
        graph,
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        acknowledge_governance_change=acknowledge,
        actor=_ACTOR,
        base_url="https://ps.example.com",
        store=store,
    )


def test_preview_names_edges_obligations_and_case_and_writes_nothing() -> None:
    graph = ScriptedMergeGraph()

    preview = preview_capability_merge(graph, survivor_id=SURVIVOR, absorbed_id=ABSORBED)

    assert preview.policy_case == 1
    assert preview.edges_to_move.requires == ("obl_1", "obl_2")
    assert preview.edges_to_move.covers == ("pa_1",)
    assert preview.obligations_affected == 2
    assert graph.write_calls == []


def test_approval_row_is_bound_to_the_exact_pair_and_state_digest() -> None:
    graph = ScriptedMergeGraph()
    store = FakeApprovalStore()

    approval = _create(graph, store)

    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    assert row.tool_name == "merge-capabilities"
    assert row.normalized_args == {
        "survivor_id": SURVIVOR,
        "absorbed_id": ABSORBED,
        "acknowledge_governance_change": False,
        "state_digest": approval.preview.state_digest,
    }
    assert (row.actor_subject, row.actor_issuer) == _ACTOR
    assert row.status == "pending"
    assert row.display_summary["absorbed_id"] == ABSORBED
    assert approval.approval_url.startswith(
        f"https://ps.example.com/approvals/{approval.pending_approval_id}#"
    )
    assert approval.expires_at == row.expires_at.isoformat()
    assert graph.write_calls == []


def test_a_swapped_pair_yields_a_different_signing_challenge() -> None:
    graph = ScriptedMergeGraph()
    store = FakeApprovalStore()
    first = _create(graph, store)
    second = _create(graph, store, survivor_id=ABSORBED, absorbed_id=SURVIVOR)

    rows = [store.get_by_id(a.pending_approval_id) for a in (first, second)]

    assert rows[0] is not None
    assert rows[1] is not None
    assert rows[0].normalized_args != rows[1].normalized_args
    assert _compute_sign_challenge(rows[0]) != _compute_sign_challenge(rows[1])


def test_the_state_digest_is_part_of_what_is_signed() -> None:
    graph = ScriptedMergeGraph()
    store = FakeApprovalStore()
    first = _create(graph, store)
    graph.requires.append(["obl_9", ABSORBED])
    second = _create(graph, store)

    assert first.preview.state_digest != second.preview.state_digest


@pytest.mark.parametrize(
    ("survivor_id", "absorbed_id"),
    [(SURVIVOR, SURVIVOR), (SURVIVOR, "cap_missing"), ("cap_missing", ABSORBED)],
)
def test_self_and_nonexistent_sides_are_rejected_with_no_approval(
    survivor_id: str, absorbed_id: str
) -> None:
    graph = ScriptedMergeGraph()
    store = FakeApprovalStore()

    with pytest.raises(GraphCleanupValidationError):
        _create(graph, store, survivor_id=survivor_id, absorbed_id=absorbed_id)

    assert approval_row_count(store) == 0
    assert graph.write_calls == []


@pytest.mark.parametrize("side", [0, 1])
def test_a_tombstone_on_either_side_is_rejected_with_no_approval(side: int) -> None:
    graph = ScriptedMergeGraph()
    graph.nodes[side][2] = "merged"
    store = FakeApprovalStore()

    with pytest.raises(GraphCleanupValidationError, match="merged"):
        _create(graph, store)

    assert approval_row_count(store) == 0


def test_two_differently_governed_capabilities_are_rejected_with_no_approval() -> None:
    graph = ScriptedMergeGraph(
        governors=[[SURVIVOR, "pol_1", "P", "approved"], [ABSORBED, "pol_2", "Q", "approved"]]
    )
    store = FakeApprovalStore()

    with pytest.raises(GraphCleanupValidationError, match="release-capability-governance"):
        _create(graph, store)

    assert approval_row_count(store) == 0
