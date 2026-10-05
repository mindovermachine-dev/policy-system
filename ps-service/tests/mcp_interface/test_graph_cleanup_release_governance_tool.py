"""`release-capability-governance` (preview + approval) through the MCP tool (issue #190, slice 14).

AC-BI-013 (draft policy: preview, no writes, approval bound to capability and policy), AC-BI-014 /
D12 (approved, deprecated, proposed rejected with a pointer, no approval), AC-BI-018 (sanitised).
Gate behaviour is in `test_graph_cleanup_authz_gate.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

import pytest
from authz._fakes import FakeAccessRoleStore
from graph_cleanup._fakes import (
    SURVIVOR,
    FakeApprovalStore,
    ScriptedReleaseGraph,
    approval_row_count,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from ps_service.authz.models import AccessRole
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

_OWNER = "owner"
_OFFICER = "officer"
_OTHER = "other-officer"
_ISSUER = "https://issuer.example.com/"


@contextlib.contextmanager
def _actor(sub: str) -> Generator[None]:
    token = AccessToken(token="t", client_id="c", scopes=[], subject=sub, claims={"iss": _ISSUER})
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def _factory(value: object) -> Callable[..., object]:
    def _make(*_args: object, **_kwargs: object) -> object:
        return value

    return _make


def _officers() -> FakeAccessRoleStore:
    store = FakeAccessRoleStore(expected_owner=(_OWNER, _ISSUER))
    store.bootstrap_first_owner((_OWNER, _ISSUER))
    for sub in (_OFFICER, _OTHER):
        store.grant(
            actor=(_OWNER, _ISSUER),
            target=(sub, _ISSUER),
            access_role=AccessRole.COMPLIANCE_OFFICER,
        )
    return store


def _opener_for(graph: ScriptedReleaseGraph) -> Callable[[object], ScriptedReleaseGraph]:
    def _open(_config: object) -> ScriptedReleaseGraph:
        return graph

    return _open


class _Env:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, graph: ScriptedReleaseGraph) -> None:
        self.graph = graph
        self.pending = FakeApprovalStore()
        monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _factory(_officers()))
        monkeypatch.setattr(mcp_server, "PsycopgPendingApprovalStore", _factory(self.pending))
        monkeypatch.setattr(
            "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
            _factory(_opener_for(graph)),
        )

    def call(self, name: str, args: dict[str, object], *, sub: str = _OFFICER) -> str:
        with _actor(sub):
            result = asyncio.run(mcp_server.server.call_tool(name, args))
        assert isinstance(result, CallToolResult)
        block = result.content[0]
        assert isinstance(block, TextContent)
        return block.text


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    configure()
    return _Env(monkeypatch, ScriptedReleaseGraph())


_ARGS: dict[str, object] = {"capability_id": SURVIVOR}


def test_a_draft_policy_returns_the_preview_and_a_pending_approval_and_writes_nothing(
    env: _Env,
) -> None:
    body = json.loads(env.call("release-capability-governance", _ARGS))

    assert set(body) == {"preview", "pending_approval_id", "approval_url", "expires_at"}
    preview = body["preview"]
    assert preview["capability_id"] == SURVIVOR
    assert preview["policy_id"] == "pol_1"
    assert preview["policy_title"] == "Incident Policy"
    assert preview["policy_status"] == "draft"
    assert preview["governed_set_after"] == ["cap_other"]
    assert body["approval_url"].startswith(
        f"http://unknown/approvals/{body['pending_approval_id']}#"
    )
    row = env.pending.get_by_id(body["pending_approval_id"])
    assert row is not None
    assert (row.actor_subject, row.actor_issuer) == (_OFFICER, _ISSUER)
    assert row.tool_name == "release-capability-governance"
    assert row.normalized_args == {
        "capability_id": SURVIVOR,
        "policy_id": "pol_1",
        "state_digest": preview["state_digest"],
    }
    assert env.graph.release_calls == []


@pytest.mark.parametrize("status", ["approved", "deprecated"])
def test_an_approved_or_deprecated_policy_is_rejected_with_a_lifecycle_pointer_and_no_approval(
    env: _Env, status: str
) -> None:
    env.graph.governors = [[SURVIVOR, "pol_1", "Incident Policy", status]]

    text = env.call("release-capability-governance", _ARGS)

    assert text.startswith("error: ")
    assert "Incident Policy" in text
    assert status in text
    assert "policy lifecycle" in text
    assert "fork carries the whole governed set" in text
    assert approval_row_count(env.pending) == 0
    assert env.graph.release_calls == []


def test_a_proposed_policy_points_at_revert_policy_to_draft(env: _Env) -> None:
    env.graph.governors = [[SURVIVOR, "pol_1", "Incident Policy", "proposed"]]

    text = env.call("release-capability-governance", _ARGS)

    assert "revert-policy-to-draft" in text
    assert approval_row_count(env.pending) == 0


def test_an_ungoverned_capability_is_rejected_with_no_approval(env: _Env) -> None:
    env.graph.governors = [["cap_other", "pol_9", "Other", "draft"]]

    text = env.call("release-capability-governance", _ARGS)

    assert text.startswith("error: ")
    assert "not governed" in text
    assert approval_row_count(env.pending) == 0


def test_a_nonexistent_capability_is_rejected_with_no_approval(env: _Env) -> None:
    text = env.call("release-capability-governance", {"capability_id": "cap_missing"})

    assert text.startswith("error: ")
    assert "does not exist" in text
    assert approval_row_count(env.pending) == 0


def test_a_tombstone_is_rejected_with_no_approval(env: _Env) -> None:
    env.graph.nodes[0][2] = "merged"

    text = env.call("release-capability-governance", _ARGS)

    assert text.startswith("error: ")
    assert "merged" in text
    assert approval_row_count(env.pending) == 0


def test_an_unreachable_graph_returns_the_sanitised_error(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    environment = _Env(monkeypatch, ScriptedReleaseGraph())

    def _boom(_config: object) -> object:
        message = "connection refused to 10.0.0.1:6379"
        raise ConnectionError(message)

    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
        _factory(_boom),
    )

    text = environment.call("release-capability-governance", _ARGS)

    assert text == "error: the policy graph database is not reachable"


def test_check_approval_reports_a_release_approval_to_its_creator_only(env: _Env) -> None:
    created = json.loads(env.call("release-capability-governance", _ARGS))
    approval_id = created["pending_approval_id"]

    mine = json.loads(env.call("check-cleanup-approval", {"pending_approval_id": approval_id}))
    theirs = env.call("check-cleanup-approval", {"pending_approval_id": approval_id}, sub=_OTHER)

    assert mine == {
        "pending_approval_id": approval_id,
        "status": "pending",
        "tool_name": "release-capability-governance",
        "outcome": None,
    }
    assert theirs == f"error: no pending approval with id {approval_id!r}"
