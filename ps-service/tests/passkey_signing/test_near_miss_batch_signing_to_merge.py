"""Signing ceremony to near-miss batch execution over HTTP.

The real approval service, signing router, real WebAuthn verification, executor registry, batch
executor, per-kind merge executor, planner and gateway seam run; only the graph (the gateway rig's
in-memory log and FalkorDB fakes), the stores and the role grants are fakes.
"""

from __future__ import annotations

import copy
import time
from datetime import timedelta
from typing import TYPE_CHECKING, cast

import pytest
from authz._fakes import FakeAccessRoleStore
from fastapi.testclient import TestClient
from graph_cleanup._fakes import (
    ABSORBED,
    SURVIVOR,
    FakeApprovalStore,
    expire_approval,
    tamper_approval_args,
)
from graph_cleanup._gateway_fakes import CleanupRig
from graph_cleanup._near_miss_batch_worlds import (
    seed_capability_pair_with_review,
    seed_policy_governing,
    seed_role_pair_with_review,
)
from graph_gateway._fakes import close_all_rigs

from passkey_signing._fakes import FakeSigningCredentialStore
from passkey_signing._harness import (
    ACTOR_ISSUER,
    ACTOR_SUBJECT,
    ORIGIN,
    RP_ID,
    Authenticator,
    enroll,
    sign_challenge,
)
from ps_service.api.dependencies import provide_pending_approval_store
from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.near_miss_batch import create_near_miss_merge_batch
from ps_service.logging import configure
from ps_service.main import create_app
from ps_service.passkey_signing.router import provide_signing_credential_store

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from ps_service.graph_gateway.system_audit import SystemAuditRecorder
    from ps_service.passkey_signing.models import PendingApprovalRow

_OWNER = ("owner", "https://issuer.example.com/")
_ACTOR = (ACTOR_SUBJECT, ACTOR_ISSUER)
_REVIEW = "review_aaa"
_OLD = "2020-01-01T00:00:00+00:00"
_NEW = "2024-01-01T00:00:00+00:00"
_APP_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=True,
    authentik_api_token="t",
    authentik_base_url="https://authentik.example.com",
)


class _World:
    def __init__(self) -> None:
        self.rig = CleanupRig()
        self.rig.seed_capability_merge_world()
        self.rig.seed_near_miss_review(
            _REVIEW,
            incoming_id=ABSORBED,
            existing_id=SURVIVOR,
            incoming_created_at=_NEW,
            existing_created_at=_OLD,
        )
        self.audit = self.rig.audit
        self.pending = FakeApprovalStore()
        self.credentials = FakeSigningCredentialStore()
        self.roles = FakeAccessRoleStore(expected_owner=_OWNER)
        self.roles.bootstrap_first_owner(_OWNER)
        self.roles.grant(actor=_OWNER, target=_ACTOR, access_role=AccessRole.COMPLIANCE_OFFICER)
        self.authenticator = Authenticator()
        enroll(self.credentials, self.authenticator)
        app = create_app(_APP_CONFIG)
        app.dependency_overrides[provide_pending_approval_store] = lambda: self.pending
        app.dependency_overrides[provide_signing_credential_store] = lambda: self.credentials
        self.client = TestClient(app)

    def approve(
        self, review_ids: list[str] | None = None, *, acknowledged: list[str] | None = None
    ) -> tuple[PendingApprovalRow, str]:
        approval = create_near_miss_merge_batch(
            self.rig.read,
            review_ids=review_ids if review_ids is not None else [_REVIEW],
            acknowledged_review_ids=acknowledged or [],
            actor=_ACTOR,
            base_url="https://ps.example.com",
            store=self.pending,
        )
        row = self.pending.get_by_id(approval.pending_approval_id)
        assert row is not None
        return row, approval.approval_url.rsplit("#", 1)[1]

    def sign(self, row: PendingApprovalRow, code: str, *, sign_count: int = 1):
        assertion = self.authenticator.build_assertion(
            rp_id=RP_ID, origin=ORIGIN, challenge=sign_challenge(row), sign_count=sign_count
        )
        return self.client.post(
            f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": assertion}
        )


def _factory(value: object) -> Callable[..., object]:
    def _make(*_args: object, **_kwargs: object) -> object:
        return value

    return _make


def _opener_for(graph: object) -> Callable[[object], object]:
    def _open(_config: object) -> object:
        return graph

    return _open


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    configure()
    built = _World()
    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
        _factory(_opener_for(built.rig.read)),
    )
    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.PsycopgAuditStore", _factory(built.audit)
    )
    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.PsycopgAccessRoleStore", _factory(built.roles)
    )
    install_graph_write_gateway(
        built.rig.gateway, system_audit=lambda _config: cast("SystemAuditRecorder", object())
    )
    yield built
    install_graph_write_gateway(None)
    close_all_rigs()


def test_importing_the_app_registers_the_batch_executor() -> None:
    from ps_service.passkey_signing.executors import resolve_approval_executor

    assert resolve_approval_executor("merge-near-misses") is not None


def test_signing_a_batch_of_one_merges_the_pair_deletes_the_review_and_stores_the_outcome(
    world: _World,
) -> None:
    row, code = world.approve()

    response = world.sign(row, code)

    expected_entry = {
        "review_id": _REVIEW,
        "kind": "Capability",
        "state": "merged",
        "winner_id": SURVIVOR,
        "loser_id": ABSORBED,
        "error": None,
    }
    assert response.status_code == 200
    assert response.json() == {
        "status": "signed",
        "merges": [expected_entry],
        "merged_count": 1,
        "failed_count": 0,
    }
    assert ("Capability", ABSORBED) not in world.rig.graph.nodes
    assert world.rig.graph.nodes[("MergedCapability", ABSORBED)]["merged_into"] == SURVIVOR
    assert ("PendingReview", _REVIEW) not in world.rig.graph.nodes
    stored = world.pending.get_by_id(row.id)
    assert stored is not None
    assert stored.outcome == {"merges": [expected_entry], "merged_count": 1, "failed_count": 0}


def test_the_applied_merge_row_precedes_the_graph_change_and_names_the_review_and_approval(
    world: _World,
) -> None:
    row, code = world.approve()

    assert world.sign(row, code).status_code == 200

    assert world.rig.order() == ["audit:applied", "log_append"]
    [recorded] = world.audit.rows
    assert recorded.action == "capability.merge"
    assert recorded.outcome == "applied"
    assert (recorded.actor_subject, recorded.actor_issuer) == _ACTOR
    assert recorded.resource_id == ABSORBED
    assert recorded.details["review_id"] == _REVIEW
    assert recorded.details["approval_id"] == row.id
    assert recorded.details["survivor_id"] == SURVIVOR


def test_signing_a_mixed_batch_merges_every_pair_and_deletes_every_review(world: _World) -> None:
    capability = seed_capability_pair_with_review(world.rig, 1)
    role = seed_role_pair_with_review(world.rig, 1)
    row, code = world.approve([role.review_id, capability.review_id, _REVIEW])

    response = world.sign(row, code)

    assert response.status_code == 200
    body = response.json()
    assert [(m["review_id"], m["state"]) for m in body["merges"]] == [
        (role.review_id, "merged"),
        (capability.review_id, "merged"),
        (_REVIEW, "merged"),
    ]
    assert (body["merged_count"], body["failed_count"]) == (3, 0)
    assert ("Role", role.absorbed_id) not in world.rig.graph.nodes
    assert (
        world.rig.graph.nodes[("MergedRole", role.absorbed_id)]["merged_into"] == role.survivor_id
    )
    assert ("Capability", capability.absorbed_id) not in world.rig.graph.nodes
    assert ("Capability", ABSORBED) not in world.rig.graph.nodes
    for review_id in (role.review_id, capability.review_id, _REVIEW):
        assert ("PendingReview", review_id) not in world.rig.graph.nodes
    assert [(r.action, r.details["review_id"]) for r in world.audit.rows] == [
        ("role.merge", role.review_id),
        ("capability.merge", capability.review_id),
        ("capability.merge", _REVIEW),
    ]
    assert {r.details["approval_id"] for r in world.audit.rows} == {row.id}
    assert len(world.rig.logged_groups) == 3


def test_two_disjoint_merges_do_not_stale_each_other(world: _World) -> None:
    first = seed_capability_pair_with_review(world.rig, 1)
    second = seed_capability_pair_with_review(world.rig, 2)
    row, code = world.approve([first.review_id, second.review_id])

    response = world.sign(row, code)

    assert [m["state"] for m in response.json()["merges"]] == ["merged", "merged"]


def test_a_signed_batch_of_fifty_reviews_merges_them_all(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    review_ids = [seed_capability_pair_with_review(world.rig, n).review_id for n in range(25)]
    review_ids += [seed_role_pair_with_review(world.rig, n).review_id for n in range(25)]
    row, code = world.approve(review_ids)

    started = time.perf_counter()
    response = world.sign(row, code)
    elapsed = time.perf_counter() - started

    assert response.json()["merged_count"] == 50
    with capsys.disabled():
        print(f"\nsigned batch of 50 merges took {elapsed:.2f}s")  # noqa: T201


def test_an_item_signed_without_its_acknowledgment_is_refused_by_the_executor_with_a_failed_row(
    world: _World,
) -> None:
    governed = seed_capability_pair_with_review(world.rig, 1)
    seed_policy_governing(world.rig, "pol_1", governed.absorbed_id)
    row, code = world.approve([governed.review_id, _REVIEW], acknowledged=[governed.review_id])
    merges = _signed_items(row)
    tamper_approval_args(
        world.pending,
        row.id,
        {"merges": [{**merges[0], "acknowledge_governance_change": False}, merges[1]]},
    )
    tampered = world.pending.get_by_id(row.id)
    assert tampered is not None

    response = world.sign(tampered, code)

    body = response.json()
    assert [(m["review_id"], m["state"]) for m in body["merges"]] == [
        (governed.review_id, "failed"),
        (_REVIEW, "merged"),
    ]
    assert body["merges"][0]["error"]
    assert ("Capability", governed.absorbed_id) in world.rig.graph.nodes
    assert ("PendingReview", governed.review_id) in world.rig.graph.nodes
    assert [(r.action, r.outcome, r.resource_id) for r in world.audit.rows] == [
        ("capability.merge", "failed", governed.absorbed_id),
        ("capability.merge", "applied", ABSORBED),
    ]


def _signed_items(row: PendingApprovalRow) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", row.normalized_args["merges"])


def _graph_state(world: _World) -> object:
    return copy.deepcopy(dict(world.rig.graph.nodes)), set(world.rig.graph.edges)


def test_a_credential_of_another_actor_cannot_sign_gets_the_unknown_id_response_and_is_audited(
    world: _World,
) -> None:
    row, code = world.approve()
    other = Authenticator()
    enroll(world.credentials, other, actor_subject="someone-else")
    assertion = other.build_assertion(
        rp_id=RP_ID, origin=ORIGIN, challenge=sign_challenge(row), sign_count=1
    )
    before = _graph_state(world)

    foreign = world.client.post(
        f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": assertion}
    )
    unknown = world.client.post(
        "/approvals/no-such-approval/sign/verify", json={"code": code, "credential": assertion}
    )

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()
    assert _graph_state(world) == before
    [refused] = world.audit.rows
    assert (refused.action, refused.outcome) == ("capability.merge", "failed")
    assert (refused.actor_subject, refused.details["reason_code"]) == (
        "someone-else",
        "signer_not_bound_actor",
    )
    stored = world.pending.get_by_id(row.id)
    assert stored is not None
    assert stored.status == "signed"
    assert world.sign(row, code).status_code == 404
    assert _graph_state(world) == before


def _tampered(item: dict[str, object], field: str) -> dict[str, object]:
    changes: dict[str, object] = {
        "review_id": "review_other",
        "survivor_id": ABSORBED,
        "absorbed_id": SURVIVOR,
        "state_digest": "0" * 64,
        "acknowledge_governance_change": True,
    }
    return {**item, field: changes[field]}


@pytest.mark.parametrize(
    "field",
    ["review_id", "survivor_id", "absorbed_id", "state_digest", "acknowledge_governance_change"],
)
def test_changing_any_item_after_creation_invalidates_the_signature(
    world: _World, field: str
) -> None:
    other = seed_capability_pair_with_review(world.rig, 1)
    row, code = world.approve([_REVIEW, other.review_id])
    merges = _signed_items(row)
    tamper_approval_args(
        world.pending, row.id, {"merges": [_tampered(merges[0], field), merges[1]]}
    )
    before = _graph_state(world)

    response = world.sign(row, code)

    assert response.status_code == 404
    assert _graph_state(world) == before
    assert world.audit.rows == []


def test_changing_the_order_of_the_items_after_creation_invalidates_the_signature(
    world: _World,
) -> None:
    other = seed_capability_pair_with_review(world.rig, 1)
    row, code = world.approve([_REVIEW, other.review_id])
    merges = _signed_items(row)
    tamper_approval_args(world.pending, row.id, {"merges": list(reversed(merges))})
    before = _graph_state(world)

    response = world.sign(row, code)

    assert response.status_code == 404
    assert _graph_state(world) == before
    assert world.audit.rows == []


def test_dropping_or_adding_an_item_after_creation_invalidates_the_signature(
    world: _World,
) -> None:
    other = seed_capability_pair_with_review(world.rig, 1)
    row, code = world.approve([_REVIEW, other.review_id])
    merges = _signed_items(row)
    tamper_approval_args(world.pending, row.id, {"merges": merges[:1]})
    before = _graph_state(world)

    response = world.sign(row, code)

    assert response.status_code == 404
    assert _graph_state(world) == before


def test_signing_after_expiry_runs_no_merge_and_leaves_the_graph_unchanged(world: _World) -> None:
    row, code = world.approve()
    expire_approval(world.pending, row.id, expired_for=timedelta(seconds=1))
    before = _graph_state(world)

    options = world.client.post(f"/approvals/{row.id}/sign/options", json={"code": code})
    response = world.sign(row, code)

    assert options.status_code == 404
    assert response.status_code == 404
    assert _graph_state(world) == before
    assert world.audit.rows == []
    assert world.rig.logged_groups == []


def test_a_signer_who_lost_the_compliance_officer_grant_gets_a_named_refusal_for_every_item(
    world: _World,
) -> None:
    other = seed_capability_pair_with_review(world.rig, 1)
    role = seed_role_pair_with_review(world.rig, 1)
    row, code = world.approve([_REVIEW, other.review_id, role.review_id])
    world.roles.revoke(actor=_OWNER, target=_ACTOR, access_role=AccessRole.COMPLIANCE_OFFICER)
    before = _graph_state(world)

    response = world.sign(row, code)

    body = response.json()
    assert [m["state"] for m in body["merges"]] == ["failed", "failed", "failed"]
    assert [m["review_id"] for m in body["merges"]] == [_REVIEW, other.review_id, role.review_id]
    for merge in body["merges"]:
        assert "no longer holds the required role" in merge["error"]
    assert (body["merged_count"], body["failed_count"]) == (0, 3)
    assert _graph_state(world) == before
    assert [(r.outcome, r.details["reason_code"]) for r in world.audit.rows] == [
        ("failed", "actor_not_authorised")
    ] * 3


def test_the_approval_is_consumed_after_a_refused_execution(world: _World) -> None:
    row, code = world.approve()
    world.roles.revoke(actor=_OWNER, target=_ACTOR, access_role=AccessRole.COMPLIANCE_OFFICER)
    assert world.sign(row, code).status_code == 200
    world.roles.grant(actor=_OWNER, target=_ACTOR, access_role=AccessRole.COMPLIANCE_OFFICER)
    before = _graph_state(world)

    replay = world.sign(row, code, sign_count=2)

    assert replay.status_code == 404
    assert _graph_state(world) == before
    stored = world.pending.get_by_id(row.id)
    assert stored is not None
    assert stored.status == "signed"
