"""`near_misses_resolve(merge)` on a `Role` review is an entry point to the `merge-roles` approval.

Over a `CleanupRig` (real reader, planner, executor and gateway rig; only the stores and the
signing ceremony are doubles) these tests drive the MCP tools as the near-miss reviewer would: the
`ComplianceOfficer` gate shared with Capability reviews, the Role preview plus approval link, the
refused governance acknowledgment, the signed `review_id`, a signer who lost the grant, and
`near_misses_check_approval` reporting the winner and the loser.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import TYPE_CHECKING

import pytest
from authz._fakes import FakeAccessRoleStore, RaisingAccessRoleStore
from graph_cleanup._fakes import (
    ROLE_ABSORBED,
    ROLE_SURVIVOR,
    FakeApprovalStore,
    approval_row_count,
)
from graph_cleanup._gateway_fakes import CleanupReadGraph, CleanupRig
from graph_cleanup._mcp_calls import call_tool, factory
from graph_cleanup._merge_runs import CONFIG, OFFICER, OWNER, roles
from graph_gateway._fakes import close_all_rigs

from mcp_interface.test_access_role_tools import (
    _override,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
    _server,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
)
from ps_service.authz.models import AccessRole
from ps_service.graph_cleanup.executors import execute_role_merge
from ps_service.logging import configure

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.authz.store import AccessRoleStore

_REVIEW = "review_role_aaa"
_UNAUTHENTICATED = "error: graph cleanup requires a real authenticated caller"
_DENIED = "error: You do not have the required access role for this action."


class _Env:
    """A rig with two Roles of one instrument and a `Role` review; tools wired to it.

    The incoming Role of the review is `ROLE_SURVIVOR`, so it is the survivor.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        rig: CleanupRig,
        *,
        access_roles: object | None = None,
    ) -> None:
        self.rig = rig
        self.store = FakeApprovalStore()
        rig.seed_role_merge_world()
        rig.seed_near_miss_review(
            _REVIEW, incoming_id=ROLE_SURVIVOR, existing_id=ROLE_ABSORBED, kind="Role"
        )

        def _open(_config: object) -> CleanupReadGraph:
            return rig.read

        _override(
            monkeypatch,
            role_store_factory=factory(access_roles or roles()),
            pending_approval_store_factory=factory(self.store),
            graph_factory=_open,
        )

    def snapshot(self) -> object:
        return copy.deepcopy(dict(self.rig.graph.nodes)), set(self.rig.graph.edges)

    def resolve_merge(
        self, *, who: tuple[str, str] = OFFICER, acknowledge: bool | None = None
    ) -> str:
        args: dict[str, object] = {"review_id": _REVIEW, "decision": "merge"}
        if acknowledge is not None:
            args["acknowledge_governance_change"] = acknowledge
        return call_tool("near_misses_resolve", args, who)

    def check(self, approval_id: str, *, who: tuple[str, str] = OFFICER) -> str:
        return call_tool("near_misses_check_approval", {"pending_approval_id": approval_id}, who)

    def sign_and_execute(
        self, approval_id: str, *, access_roles: AccessRoleStore | None = None
    ) -> dict[str, object]:
        """Sign the way the router does: mark signed, run the executor, store the outcome."""
        assert self.store.mark_signed(approval_id)
        signed = self.store.get_by_id(approval_id)
        assert signed is not None
        outcome = execute_role_merge(signed, CONFIG, self.rig.dependencies(access_roles or roles()))
        self.store.set_outcome(approval_id, outcome)
        return outcome


@pytest.fixture
def cleanup_rig() -> Iterator[CleanupRig]:
    rig = CleanupRig()
    yield rig
    close_all_rigs()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, cleanup_rig: CleanupRig) -> _Env:
    configure()
    return _Env(monkeypatch, cleanup_rig)


# --- the ComplianceOfficer gate ------------------------------------------------------------


@pytest.mark.parametrize("role", [None, AccessRole.SYSTEM_ADMIN, AccessRole.SYSTEM_OWNER])
def test_a_caller_without_an_explicit_compliance_officer_grant_gets_an_error_and_no_approval(
    monkeypatch: pytest.MonkeyPatch, cleanup_rig: CleanupRig, role: AccessRole | None
) -> None:
    configure()
    access_roles = FakeAccessRoleStore(expected_owner=OWNER)
    access_roles.bootstrap_first_owner(OWNER)
    caller = ("plain-user", OWNER[1])
    if role is AccessRole.SYSTEM_OWNER:
        caller = OWNER
    elif role is AccessRole.SYSTEM_ADMIN:
        access_roles.grant(actor=OWNER, target=caller, access_role=role)
    environment = _Env(monkeypatch, cleanup_rig, access_roles=access_roles)
    before = environment.snapshot()

    text = environment.resolve_merge(who=caller)

    assert text == _DENIED
    assert approval_row_count(environment.store) == 0
    assert environment.snapshot() == before


@pytest.mark.parametrize("bypass", [False, True])
def test_no_real_session_gets_an_error_and_no_approval_even_under_the_local_test_bypass(
    monkeypatch: pytest.MonkeyPatch, cleanup_rig: CleanupRig, *, bypass: bool
) -> None:
    if bypass:
        monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    environment = _Env(monkeypatch, cleanup_rig, access_roles=RaisingAccessRoleStore())
    before = environment.snapshot()

    text = asyncio.run(
        _server().call_tool("near_misses_resolve", {"review_id": _REVIEW, "decision": "merge"})
    )

    assert _UNAUTHENTICATED in str(text)
    assert approval_row_count(environment.store) == 0
    assert environment.snapshot() == before


# --- the merge --------------------------------------------------------------------------------


def test_merge_returns_the_role_preview_and_an_approval_link_and_leaves_the_graph_unchanged(
    env: _Env,
) -> None:
    before = env.snapshot()

    body = json.loads(env.resolve_merge())

    assert set(body) == {"preview", "pending_approval_id", "approval_url", "expires_at"}
    assert body["preview"]["survivor_id"] == ROLE_SURVIVOR
    assert body["preview"]["absorbed_id"] == ROLE_ABSORBED
    assert body["preview"]["state_digest"]
    assert body["approval_url"].startswith(
        f"http://unknown/approvals/{body['pending_approval_id']}#"
    )
    row = env.store.get_by_id(body["pending_approval_id"])
    assert row is not None
    assert row.tool_name == "merge-roles"
    assert row.normalized_args["review_id"] == _REVIEW
    assert (row.actor_subject, row.actor_issuer) == OFFICER
    assert env.snapshot() == before


def test_acknowledging_a_governance_change_is_refused_for_a_role_review(env: _Env) -> None:
    before = env.snapshot()

    text = env.resolve_merge(acknowledge=True)

    assert text.startswith("error: ")
    assert "acknowledge_governance_change" in text
    assert approval_row_count(env.store) == 0
    assert env.snapshot() == before


def test_signing_moves_the_obligations_deletes_the_review_and_check_reports_the_pair(
    env: _Env,
) -> None:
    approval_id = json.loads(env.resolve_merge())["pending_approval_id"]

    pending = json.loads(env.check(approval_id))
    outcome = env.sign_and_execute(approval_id)
    signed = json.loads(env.check(approval_id))

    assert pending == {
        "pending_approval_id": approval_id,
        "status": "pending",
        "review_id": _REVIEW,
        "decision": "merge",
        "winner_id": None,
        "loser_id": None,
        "error": None,
    }
    assert outcome == {"survivor_id": ROLE_SURVIVOR, "absorbed_id": ROLE_ABSORBED, "merged": True}
    assert signed["status"] == "signed"
    assert (signed["winner_id"], signed["loser_id"]) == (ROLE_SURVIVOR, ROLE_ABSORBED)
    assert signed["error"] is None
    assert ("Role", ROLE_ABSORBED) not in env.rig.graph.nodes
    assert env.rig.graph.nodes[("MergedRole", ROLE_ABSORBED)]["merged_into"] == ROLE_SURVIVOR
    assert ("PendingReview", _REVIEW) not in env.rig.graph.nodes


def test_a_signer_who_loses_the_grant_before_execution_is_refused_with_a_failed_row(
    env: _Env,
) -> None:
    """Sign, revoke ComplianceOfficer, execute: refused, failed row, graph unchanged."""
    approval_id = json.loads(env.resolve_merge())["pending_approval_id"]
    revoked = roles()
    revoked.revoke(actor=OWNER, target=OFFICER, access_role=AccessRole.COMPLIANCE_OFFICER)
    before = env.snapshot()

    outcome = env.sign_and_execute(approval_id, access_roles=revoked)

    assert "no longer holds the required role" in str(outcome["error"])
    assert env.snapshot() == before
    assert ("PendingReview", _REVIEW) in env.rig.graph.nodes
    [recorded] = env.rig.audit.rows
    assert (recorded.action, recorded.outcome, recorded.resource_id) == (
        "role.merge",
        "failed",
        ROLE_ABSORBED,
    )
    assert recorded.details["reason_code"] == "actor_not_authorised"
    assert recorded.details["review_id"] == _REVIEW
    assert json.loads(env.check(approval_id))["error"] == outcome["error"]


def test_merge_of_a_role_review_whose_side_was_absorbed_is_an_error_naming_the_review(
    env: _Env,
) -> None:
    del env.rig.graph.nodes[("Role", ROLE_ABSORBED)]  # absorbed by a Role merge since the listing

    text = env.resolve_merge()

    assert text.startswith("error: ")
    assert _REVIEW in text
    assert "stale" in text
    assert approval_row_count(env.store) == 0
