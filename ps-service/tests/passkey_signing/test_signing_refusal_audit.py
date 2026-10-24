"""A cleanup approval signed with another enrolled actor's passkey: audited, voided, never run.

Every passkey-approved cleanup action is driven through the real signing router, the real
WebAuthn verification and the refusal recorder graph cleanup registers. The other actor's
assertion is genuinely valid for the approval's challenge, so it verifies; the approval is then
consumed with a fixed error outcome, and one `failed` row per action is recorded under the
approval's own action with the verified signer as actor and `signer_not_bound_actor` as the reason.
The graph and its log stay untouched, and the bound actor can no longer sign the approval.
An assertion that does not verify leaves no row and leaves the approval pending.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Protocol

import pytest
from graph_cleanup._near_miss_batch_worlds import seed_role_pair_with_review
from graph_cleanup.test_execute_capability_split import world as capability_split_world
from webauthn.helpers import bytes_to_base64url

from passkey_signing._harness import ACTOR_ISSUER, ORIGIN, RP_ID, Authenticator, enroll
from passkey_signing._harness import sign_challenge as _sign_challenge
from passkey_signing.test_graph_cleanup_obligation_signing_to_merge import (
    world as obligation_merge_world,
)
from passkey_signing.test_graph_cleanup_release_signing import world as release_world
from passkey_signing.test_graph_cleanup_role_signing_to_merge import world as role_merge_world
from passkey_signing.test_graph_cleanup_role_signing_to_unmerge import (
    world as role_unmerge_world,
)
from passkey_signing.test_graph_cleanup_signing_to_merge import world as capability_merge_world
from passkey_signing.test_graph_cleanup_unmerge_obligation_signing import (
    world as obligation_unmerge_world,
)
from passkey_signing.test_graph_cleanup_unmerge_signing import world as capability_unmerge_world
from passkey_signing.test_near_miss_batch_signing_to_merge import world as near_miss_batch_world
from ps_service.graph_cleanup.executors import CLEANUP_TOOL_NAMES
from ps_service.graph_cleanup.near_miss_batch_models import TOOL_MERGE_NEAR_MISSES
from ps_service.passkey_signing.executors import resolve_signing_refusal_recorder

if TYPE_CHECKING:
    from ps_service.passkey_signing.models import PendingApprovalRow

__all__ = [  # the imported `world` fixtures are used by name through `flow`
    "capability_merge_world",
    "capability_split_world",
    "capability_unmerge_world",
    "near_miss_batch_world",
    "obligation_merge_world",
    "obligation_unmerge_world",
    "release_world",
    "role_merge_world",
    "role_unmerge_world",
]

_SIGNER = ("user-2", ACTOR_ISSUER)
_REASON = "signer_not_bound_actor"
_FLOWS = [
    ("capability_merge_world", "capability.merge"),
    ("obligation_merge_world", "obligation.merge"),
    ("role_merge_world", "role.merge"),
    ("release_world", "capability.release_governance"),
    ("capability_unmerge_world", "capability.unmerge"),
    ("obligation_unmerge_world", "obligation.unmerge"),
    ("role_unmerge_world", "role.unmerge"),
    ("capability_split_world", "capability.split"),
]
_SIGNER_REFUSED_OUTCOME = {
    "error": (
        "this approval was signed by someone other than the person it was issued to and can no "
        "longer be used; ask for a new approval"
    )
}


class _World(Protocol):
    rig: _Rig
    audit: _Audit
    pending: _Pending
    credentials: object
    client: _Client

    def approve(self) -> tuple[PendingApprovalRow, str]: ...

    def sign(self, row: PendingApprovalRow, code: str) -> _Response: ...


class _BatchWorld(_World, Protocol):
    def approve(self, review_ids: list[str] | None = None) -> tuple[PendingApprovalRow, str]: ...


class _Rig(Protocol):
    graph: _Graph

    @property
    def logged_groups(self) -> list[object]: ...


class _Graph(Protocol):
    nodes: dict[object, object]
    edges: dict[object, object]


class _Audit(Protocol):
    rows: list[_Row]


class _Row(Protocol):
    outcome: str
    action: str
    actor_subject: str
    actor_issuer: str
    details: dict[str, object]


class _Pending(Protocol):
    def get_by_id(self, pending_approval_id: str) -> PendingApprovalRow | None: ...


class _Client(Protocol):
    def post(self, url: str, *, json: object) -> _Response: ...


class _Response(Protocol):
    status_code: int

    def json(self) -> dict[str, object]: ...


@pytest.fixture(params=_FLOWS, ids=[flow for flow, _ in _FLOWS])
def flow(request: pytest.FixtureRequest) -> tuple[_World, str]:
    name, action = request.param
    return request.getfixturevalue(name), action


def _enrolled_other_actor(world: _World) -> Authenticator:
    authenticator = Authenticator()
    enroll(world.credentials, authenticator, actor_subject=_SIGNER[0])  # pyright: ignore[reportArgumentType]  -- every world's credential store is the fake store
    return authenticator


def _sign_as(
    world: _World, authenticator: Authenticator, row: PendingApprovalRow, code: str
) -> _Response:
    assertion = authenticator.build_assertion(
        rp_id=RP_ID, origin=ORIGIN, challenge=_sign_challenge(row), sign_count=1
    )
    return world.client.post(
        f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": assertion}
    )


def _graph_state(world: _World) -> tuple[object, object, int]:
    return (
        copy.deepcopy(world.rig.graph.nodes),
        copy.deepcopy(world.rig.graph.edges),
        len(world.rig.logged_groups),
    )


def _assert_refused_and_voided(world: _World, row: PendingApprovalRow, response: _Response) -> None:
    assert response.status_code == 404
    stored = world.pending.get_by_id(row.id)
    assert stored is not None
    assert stored.status == "signed"
    assert stored.outcome == _SIGNER_REFUSED_OUTCOME


def _assert_failed_row(recorded: _Row, action: str, row: PendingApprovalRow) -> None:
    assert (recorded.action, recorded.outcome) == (action, "failed")
    assert (recorded.actor_subject, recorded.actor_issuer) == _SIGNER
    assert recorded.details["approval_id"] == row.id
    assert recorded.details["reason_code"] == _REASON


def test_other_actors_verified_passkey_records_failed_split_row_and_voids_approval(
    capability_split_world: _World,
) -> None:
    world = capability_split_world
    row, code = world.approve()
    other = _enrolled_other_actor(world)
    before = _graph_state(world)

    response = _sign_as(world, other, row, code)

    _assert_refused_and_voided(world, row, response)
    [recorded] = world.audit.rows
    _assert_failed_row(recorded, "capability.split", row)
    assert recorded.details["x_id"] == row.normalized_args["x_id"]
    assert _graph_state(world) == before
    assert world.sign(row, code).status_code == 404
    assert [r.outcome for r in world.audit.rows] == ["failed"]
    assert _graph_state(world) == before


def test_every_cleanup_action_records_one_failed_row_for_another_actors_verified_passkey(
    flow: tuple[_World, str],
) -> None:
    world, action = flow
    row, code = world.approve()
    other = _enrolled_other_actor(world)
    before = _graph_state(world)
    seeded_rows = len(world.audit.rows)

    response = _sign_as(world, other, row, code)

    _assert_refused_and_voided(world, row, response)
    [recorded] = world.audit.rows[seeded_rows:]
    _assert_failed_row(recorded, action, row)
    assert _graph_state(world) == before
    assert world.sign(row, code).status_code == 404
    assert [r.outcome for r in world.audit.rows[seeded_rows:]] == ["failed"]
    assert _graph_state(world) == before


def test_a_near_miss_batch_records_one_failed_row_per_signed_merge(
    near_miss_batch_world: _BatchWorld,
) -> None:
    world = near_miss_batch_world
    role = seed_role_pair_with_review(world.rig, 1)  # pyright: ignore[reportArgumentType]  -- the batch world's rig is a CleanupRig
    row, code = world.approve([role.review_id, "review_aaa"])
    other = _enrolled_other_actor(world)
    before = _graph_state(world)

    response = _sign_as(world, other, row, code)

    _assert_refused_and_voided(world, row, response)
    role_row, capability_row = world.audit.rows
    _assert_failed_row(role_row, "role.merge", row)
    _assert_failed_row(capability_row, "capability.merge", row)
    assert role_row.details["review_id"] == role.review_id
    assert capability_row.details["review_id"] == "review_aaa"
    assert _graph_state(world) == before


def test_another_actors_unverified_assertion_leaves_no_row_and_the_approval_pending(
    capability_merge_world: _World,
) -> None:
    world = capability_merge_world
    row, code = world.approve()
    other = _enrolled_other_actor(world)
    forged = {
        "id": bytes_to_base64url(other.credential_id),
        "rawId": bytes_to_base64url(other.credential_id),
        "type": "public-key",
        "response": {"clientDataJSON": "", "authenticatorData": "", "signature": ""},
    }

    response = world.client.post(
        f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": forged}
    )

    assert response.status_code == 404
    assert world.audit.rows == []
    stored = world.pending.get_by_id(row.id)
    assert stored is not None
    assert stored.status == "pending"
    assert world.sign(row, code).status_code == 200


def test_importing_the_app_registers_a_refusal_recorder_for_every_cleanup_tool(
    capability_merge_world: _World,
) -> None:
    del capability_merge_world  # builds the app, which imports every registering module
    for tool_name in sorted(CLEANUP_TOOL_NAMES | {TOOL_MERGE_NEAR_MISSES}):
        assert resolve_signing_refusal_recorder(tool_name) is not None, tool_name
