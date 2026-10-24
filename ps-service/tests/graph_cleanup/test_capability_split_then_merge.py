"""A split Capability can be merged back into the Capability it was split from.

After a split, `X` is an active Capability with no governing Policy and no resolution marker, and
`A` keeps its governing Policy. Merging `X` into `A` is therefore a case-2 merge: the preview asks
for the officer's acknowledgment, and with it the ordinary `merge-capabilities` path moves every
edge of `X` onto `A`, deletes `X` and leaves a `MergedCapability {id: X, merged_into: A}` marker.

The real split and merge services and executors run over one `CleanupRig`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from graph_gateway._fakes import close_all_rigs

from graph_cleanup._fakes import FakeApprovalStore
from graph_cleanup._gateway_fakes import SPLIT_A, SPLIT_POLICY, SPLIT_X, CleanupRig
from graph_cleanup._merge_runs import CONFIG, OFFICER, roles
from graph_cleanup._split_runs import run_capability_split, signed_split_approval
from ps_service.graph_cleanup.errors import GraphCleanupAcknowledgmentRequiredError
from ps_service.graph_cleanup.executors import execute_capability_merge
from ps_service.graph_cleanup.service import create_capability_merge_approval
from ps_service.logging import configure

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.passkey_signing.models import PendingApprovalRow

_MERGE_ACTION = "capability.merge"


@pytest.fixture(autouse=True)
def _logging_configured() -> None:  # pyright: ignore[reportUnusedFunction]  # autouse fixture
    configure()


@pytest.fixture
def rig() -> Iterator[CleanupRig]:
    """The governed split world, with `X` already split out of `A`."""
    rig = CleanupRig()
    rig.seed_capability_split_world(governed=True)
    _, row = signed_split_approval(rig)
    assert run_capability_split(rig, row=row) == {"x_id": SPLIT_X, "a_id": SPLIT_A, "split": True}
    yield rig
    close_all_rigs()


def _merge_x_into_a(rig: CleanupRig, *, acknowledged: bool) -> PendingApprovalRow:
    """A signed `merge-capabilities` approval absorbing `X` into `A`, made by the real service."""
    store = FakeApprovalStore()
    approval = create_capability_merge_approval(
        rig.read,
        survivor_id=SPLIT_A,
        absorbed_id=SPLIT_X,
        acknowledge_governance_change=acknowledged,
        actor=OFFICER,
        base_url="https://ps.example.com",
        store=store,
    )
    assert store.mark_signed(approval.pending_approval_id)
    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    return row


def _incoming(rig: CleanupRig, capability: str) -> set[tuple[str, str]]:
    """`(relationship, source id)` of every edge into `capability`."""
    return {(key[0], key[2]) for key in rig.graph.edges if key[4] == capability}


def test_merging_the_split_capability_back_needs_the_governance_acknowledgment(
    rig: CleanupRig,
) -> None:
    groups_before = len(rig.store.groups)

    with pytest.raises(GraphCleanupAcknowledgmentRequiredError) as raised:
        _merge_x_into_a(rig, acknowledged=False)

    governance = raised.value.preview.governance
    assert governance is not None
    assert governance.policy.id == SPLIT_POLICY
    assert governance.acknowledgment_required is True
    assert len(rig.store.groups) == groups_before
    assert ("Capability", SPLIT_X) in rig.graph.nodes


def test_an_acknowledged_merge_after_the_split_absorbs_x_into_a_and_leaves_a_merge_marker(
    rig: CleanupRig,
) -> None:
    edges_on_x = _incoming(rig, SPLIT_X)
    edges_on_a = _incoming(rig, SPLIT_A)
    assert ("GOVERNED_BY", SPLIT_X) not in {(k[0], k[2]) for k in rig.graph.edges}
    row = _merge_x_into_a(rig, acknowledged=True)

    outcome = execute_capability_merge(row, CONFIG, rig.dependencies(roles()))

    assert outcome["merged"] is True
    assert rig.graph.nodes[("MergedCapability", SPLIT_X)] == {"merged_into": SPLIT_A}
    assert ("Capability", SPLIT_X) not in rig.graph.nodes
    assert ("ResolvedCapability", SPLIT_X) not in rig.graph.nodes
    assert _incoming(rig, SPLIT_X) == set()
    assert _incoming(rig, SPLIT_A) == edges_on_x | edges_on_a
    assert ("GOVERNED_BY", "Capability", SPLIT_A, "Policy", SPLIT_POLICY) in {
        key[:5] for key in rig.graph.edges
    }
    [merge_row] = [r for r in rig.audit.rows if r.action == _MERGE_ACTION]
    assert merge_row.outcome == "applied"
    assert (merge_row.resource_type, merge_row.resource_id) == ("capability", SPLIT_X)
    assert merge_row.details["approval_id"] == row.id
