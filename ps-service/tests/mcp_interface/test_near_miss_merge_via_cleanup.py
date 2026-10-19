"""`near_misses_resolve(merge)` is an entry point to the cleanup `merge-capabilities` approval.

Over a `CleanupRig` (real reader, planner, executor and gateway rig; only the stores and the
signing ceremony are doubles) these tests drive the MCP tools as the near-miss reviewer would:
the `ComplianceOfficer` gate, the preview plus approval link, the Policy cases, the signed
`review_id`, and `near_misses_check_approval` reporting the winner and the loser.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from authz._fakes import FakeAccessRoleStore, RaisingAccessRoleStore
from company_merge._kinds import UNREGISTERED_KIND
from graph_cleanup._fakes import ABSORBED, SURVIVOR, FakeApprovalStore, approval_row_count
from graph_cleanup._gateway_fakes import CleanupReadGraph, CleanupRig
from graph_cleanup._mcp_calls import call_tool, factory
from graph_cleanup._merge_runs import CONFIG, OFFICER, OWNER, roles
from graph_gateway._fakes import close_all_rigs

from ps_service.authz.models import AccessRole
from ps_service.graph_cleanup.executors import execute_capability_merge
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server
from ps_service.passkey_signing.errors import PendingApprovalPersistenceError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mcp.types import Tool

    from ps_service.authz.store import AccessRoleStore
    from ps_service.company_merge.reviewable_kinds import ReviewableKind

    type ReadLines = Callable[[Path], list[dict[str, object]]]

_REVIEW = "review_aaa"
_OLD = "2020-01-01T00:00:00+00:00"
_NEW = "2024-01-01T00:00:00+00:00"
_UNAUTHENTICATED = "error: graph cleanup requires a real authenticated caller"
_DENIED = "error: You do not have the required access role for this action."


class _Env:
    """A rig world with a review of (ABSORBED incoming, SURVIVOR existing), tools wired to it."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        rig: CleanupRig,
        *,
        access_roles: object | None = None,
    ) -> None:
        self.rig = rig
        self.store = FakeApprovalStore()
        rig.seed_capability_merge_world()
        rig.seed_near_miss_review(
            _REVIEW,
            incoming_id=ABSORBED,
            existing_id=SURVIVOR,
            incoming_created_at=_NEW,
            existing_created_at=_OLD,
        )
        monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", factory(access_roles or roles()))
        monkeypatch.setattr(mcp_server, "PsycopgPendingApprovalStore", factory(self.store))

        def _open(_config: object) -> CleanupReadGraph:
            return rig.read

        monkeypatch.setattr(
            "ps_service.api.near_miss_review_orchestration."
            "build_default_near_miss_review_graph_opener",
            lambda: _open,
        )
        monkeypatch.setattr(
            "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
            lambda: _open,
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
        """Sign the way the router does: mark signed, run the executor, store the outcome.

        `access_roles` is the store the executor re-checks the signer's grant against (default:
        the signer still holds `ComplianceOfficer`).
        """
        row = self.store.get_by_id(approval_id)
        assert row is not None
        assert self.store.mark_signed(approval_id)
        signed = self.store.get_by_id(approval_id)
        assert signed is not None
        outcome = execute_capability_merge(
            signed, CONFIG, self.rig.dependencies(access_roles or roles())
        )
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


def _governed_by(env: _Env, capability_id: str) -> list[str]:
    return [
        key[4] for key in env.rig.graph.edges if key[0] == "GOVERNED_BY" and key[2] == capability_id
    ]


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
        mcp_server.server.call_tool(
            "near_misses_resolve", {"review_id": _REVIEW, "decision": "merge"}
        )
    )

    assert _UNAUTHENTICATED in str(text)
    assert approval_row_count(environment.store) == 0
    assert environment.snapshot() == before


# --- case 1: the walking skeleton ------------------------------------------------------------


def test_merge_returns_the_preview_and_an_approval_link_and_leaves_the_graph_unchanged(
    env: _Env,
) -> None:
    before = env.snapshot()

    body = json.loads(env.resolve_merge())

    assert set(body) == {"preview", "pending_approval_id", "approval_url", "expires_at"}
    assert body["preview"]["survivor_id"] == SURVIVOR
    assert body["preview"]["absorbed_id"] == ABSORBED
    assert body["preview"]["policy_case"] == 1
    assert body["preview"]["edges_to_move"]["covers"] == ["pa_1"]
    assert body["preview"]["state_digest"]
    assert body["approval_url"].startswith(
        f"http://unknown/approvals/{body['pending_approval_id']}#"
    )
    row = env.store.get_by_id(body["pending_approval_id"])
    assert row is not None
    assert row.tool_name == "merge-capabilities"
    assert row.normalized_args["review_id"] == _REVIEW
    assert (row.actor_subject, row.actor_issuer) == OFFICER
    assert env.snapshot() == before


def test_signing_the_approval_marks_the_loser_deletes_the_review_and_check_reports_the_pair(
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
    assert outcome == {"survivor_id": SURVIVOR, "absorbed_id": ABSORBED, "merged": True}
    assert signed["status"] == "signed"
    assert (signed["winner_id"], signed["loser_id"]) == (SURVIVOR, ABSORBED)
    assert signed["error"] is None
    assert ("Capability", ABSORBED) not in env.rig.graph.nodes
    assert env.rig.graph.nodes[("MergedCapability", ABSORBED)] == {"merged_into": SURVIVOR}
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
        "capability.merge",
        "failed",
        ABSORBED,
    )
    assert recorded.details["reason_code"] == "actor_not_authorised"
    assert recorded.details["review_id"] == _REVIEW
    assert recorded.details["survivor_id"] == SURVIVOR
    assert json.loads(env.check(approval_id))["error"] == outcome["error"]


def test_check_reports_the_executors_error_for_a_signed_approval_that_failed(env: _Env) -> None:
    approval_id = json.loads(env.resolve_merge())["pending_approval_id"]
    env.store.mark_signed(approval_id)
    env.store.set_outcome(approval_id, {"error": "the merge could not be applied"})

    body = json.loads(env.check(approval_id))

    assert body["status"] == "signed"
    assert (body["winner_id"], body["loser_id"]) == (None, None)
    assert body["error"] == "the merge could not be applied"


def test_check_hides_an_approval_from_another_caller_and_an_unknown_id(env: _Env) -> None:
    approval_id = json.loads(env.resolve_merge())["pending_approval_id"]

    other = env.check(approval_id, who=("someone-else", OFFICER[1]))
    unknown = env.check("nope")

    assert other == f"error: no pending approval with id {approval_id!r}"
    assert unknown == "error: no pending approval with id 'nope'"


def test_check_refuses_a_cleanup_merge_approval_that_carries_no_review_id(env: _Env) -> None:
    row, _code = env.store.create_pending_approval(
        tool_name="merge-capabilities",
        normalized_args={
            "survivor_id": SURVIVOR,
            "absorbed_id": ABSORBED,
            "acknowledge_governance_change": False,
            "state_digest": "d",
        },
        actor_subject=OFFICER[0],
        actor_issuer=OFFICER[1],
        display_summary={},
    )

    assert env.check(row.id) == f"error: no pending approval with id {row.id!r}"


# --- Policy cases -------------------------------------------------------------------------


def test_two_differently_governed_capabilities_give_an_error_naming_the_release_step(
    env: _Env,
) -> None:
    env.rig.seed_policy("pol_a", title="A", status="draft")
    env.rig.seed_policy("pol_b", title="B", status="draft")
    env.rig.seed_governs(SURVIVOR, "pol_a")
    env.rig.seed_governs(ABSORBED, "pol_b")
    before = env.snapshot()

    text = env.resolve_merge()

    assert text.startswith("error: ")
    assert "release-capability-governance" in text
    assert approval_row_count(env.store) == 0
    assert env.snapshot() == before
    assert ("PendingReview", _REVIEW) in env.rig.graph.nodes


def test_exactly_one_governed_capability_returns_the_preview_asking_for_the_acknowledgment(
    env: _Env,
) -> None:
    env.rig.seed_policy("pol_1", title="Incident Policy", status="approved")
    env.rig.seed_governs(ABSORBED, "pol_1")
    before = env.snapshot()

    body = json.loads(env.resolve_merge())

    assert set(body) == {"preview", "acknowledgment_required", "message"}
    assert body["acknowledgment_required"] is True
    assert body["preview"]["policy_case"] == 2
    assert "acknowledge_governance_change=true" in body["message"]
    assert approval_row_count(env.store) == 0
    assert env.snapshot() == before


def test_with_the_acknowledgment_flag_the_approval_is_created_and_the_winner_ends_up_governed(
    env: _Env,
) -> None:
    env.rig.seed_policy("pol_1", title="Incident Policy", status="approved")
    env.rig.seed_governs(ABSORBED, "pol_1")

    body = json.loads(env.resolve_merge(acknowledge=True))
    env.sign_and_execute(body["pending_approval_id"])

    assert body["preview"]["policy_case"] == 2
    row = env.store.get_by_id(body["pending_approval_id"])
    assert row is not None
    assert row.normalized_args["acknowledge_governance_change"] is True
    assert _governed_by(env, SURVIVOR) == ["pol_1"]


def test_an_unacknowledged_signed_approval_is_refused_by_the_executor(env: _Env) -> None:
    env.rig.seed_policy("pol_1", title="Incident Policy", status="approved")
    env.rig.seed_governs(ABSORBED, "pol_1")
    body = json.loads(env.resolve_merge(acknowledge=True))
    row = env.store.get_by_id(body["pending_approval_id"])
    assert row is not None
    forged = replace(
        row,
        status="signed",
        normalized_args={**row.normalized_args, "acknowledge_governance_change": False},
    )
    before = env.snapshot()

    outcome = execute_capability_merge(forged, CONFIG, env.rig.dependencies(roles()))

    assert "error" in outcome
    assert env.snapshot() == before


def test_merge_of_a_review_that_is_not_a_capability_review_returns_an_error(
    env: _Env, test_only_reviewable_kind: ReviewableKind
) -> None:
    label = test_only_reviewable_kind.label
    env.rig.seed_near_miss_review(
        "review_probe", incoming_id="probe_a", existing_id="probe_b", kind=label
    )

    text = call_tool(
        "near_misses_resolve", {"review_id": "review_probe", "decision": "merge"}, OFFICER
    )

    assert text.startswith("error: ")
    assert label in text
    assert approval_row_count(env.store) == 0


def test_merge_of_a_review_of_an_unregistered_kind_returns_an_error_naming_the_kind(
    env: _Env,
) -> None:
    env.rig.seed_near_miss_review(
        "review_stray", incoming_id="stray_a", existing_id="stray_b", kind=UNREGISTERED_KIND
    )

    text = call_tool(
        "near_misses_resolve", {"review_id": "review_stray", "decision": "merge"}, OFFICER
    )

    assert text.startswith("error: ")
    assert UNREGISTERED_KIND in text
    assert approval_row_count(env.store) == 0


def test_an_unknown_review_gives_the_named_not_found_error(env: _Env) -> None:
    text = call_tool("near_misses_resolve", {"review_id": "missing", "decision": "merge"}, OFFICER)

    assert text == "error: no unresolved PendingReview with id 'missing'"
    assert approval_row_count(env.store) == 0


# --- logging, audit and failure surface ---------------------------------------------------------


def test_requesting_a_merge_writes_no_audit_row_and_logs_started_then_succeeded(
    env: _Env, read_lines: ReadLines
) -> None:
    emitter = configure()

    assert "pending_approval_id" in json.loads(env.resolve_merge())

    assert env.rig.audit.rows == []
    emitter.flush()
    lines = [
        line
        for line in read_lines(resolve_default_log_path())
        if line.get("component") == "mcp_interface" and line.get("action") == "near_misses_resolve"
    ]
    assert [line["outcome"] for line in lines] == ["started", "succeeded"]
    assert len({line["run_id"] for line in lines}) == 1


def test_an_unreachable_graph_gives_the_fixed_message_and_no_approval(
    monkeypatch: pytest.MonkeyPatch, env: _Env
) -> None:
    def _unreachable(_config: object) -> object:
        message = "connection refused to 10.0.0.1:6379"
        raise ConnectionError(message)

    monkeypatch.setattr(
        "ps_service.api.near_miss_review_orchestration.build_default_near_miss_review_graph_opener",
        lambda: _unreachable,
    )

    before = env.snapshot()

    text = env.resolve_merge()

    assert text == "error: the policy graph database is not reachable"
    assert "10.0.0.1" not in text
    assert approval_row_count(env.store) == 0
    assert env.snapshot() == before


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("boom at /secret/path password=hunter2"),
        PendingApprovalPersistenceError("insert failed on host 10.1.2.3:5432 password=hunter2"),
    ],
    ids=["unexpected", "approval-store-unreachable"],
)
def test_an_unexpected_or_store_failure_gives_the_fixed_message_and_no_approval(
    monkeypatch: pytest.MonkeyPatch, env: _Env, failure: Exception
) -> None:
    """Create leg: fixed text, no seeded host/path/credential, nothing created."""

    class _RaisingStore:
        def create_pending_approval(self, **kwargs: object) -> object:
            _ = kwargs
            raise failure

    monkeypatch.setattr(mcp_server, "PsycopgPendingApprovalStore", factory(_RaisingStore()))
    before = env.snapshot()

    text = env.resolve_merge()

    assert text == "error: an unexpected error occurred"
    for secret in ("/secret/path", "hunter2", "10.1.2.3"):
        assert secret not in text
    assert approval_row_count(env.store) == 0
    assert env.snapshot() == before


# --- tool surface ----------------------------------------------------------------------------


def _tool(name: str) -> Tool:
    tools = asyncio.run(mcp_server.server.list_tools())
    [tool] = [t for t in tools if t.name == name]
    return tool


def test_the_resolve_schema_offers_the_acknowledgment_flag_and_still_hides_ctx() -> None:
    schema = _tool("near_misses_resolve").input_schema

    assert set(schema["properties"]) == {"review_id", "decision", "acknowledge_governance_change"}
    assert set(schema["required"]) == {"review_id", "decision"}


def test_the_resolve_text_describes_the_cleanup_merge_and_its_marker() -> None:
    description = _tool("near_misses_resolve").description or ""

    assert "tombstone" not in description
    assert "MERGED_INTO" not in description
    assert "`MergedCapability` marker" in description
    assert "ComplianceOfficer" in description
    assert "acknowledge_governance_change" in description


def test_the_check_text_names_the_cleanup_approval_it_reads() -> None:
    description = _tool("near_misses_check_approval").description or ""

    assert "merge-capabilities" in description
    assert "winner_id" in description
