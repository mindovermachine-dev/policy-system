"""`merge-capabilities` (preview + approval) and `check-cleanup-approval` (issue #190, slice 10).

AC-BI-005 (preview, no writes, approval bound to the pair), AC-BI-016 (rejected before any
approval), AC-BI-018 (sanitised errors). Gate behaviour is in `test_graph_cleanup_authz_gate.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

import pytest
from authz._fakes import FakeAccessRoleStore
from graph_cleanup._fakes import (
    ABSORBED,
    SURVIVOR,
    FakeApprovalStore,
    ScriptedMergeGraph,
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


def _opener_for(graph: ScriptedMergeGraph) -> Callable[[object], ScriptedMergeGraph]:
    def _open(_config: object) -> ScriptedMergeGraph:
        return graph

    return _open


class _Env:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, graph: ScriptedMergeGraph) -> None:
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
    return _Env(monkeypatch, ScriptedMergeGraph())


_MERGE_ARGS: dict[str, object] = {"survivor_id": SURVIVOR, "absorbed_id": ABSORBED}


def test_merge_returns_the_preview_and_a_pending_approval_and_writes_nothing(env: _Env) -> None:
    body = json.loads(env.call("merge-capabilities", _MERGE_ARGS))

    assert set(body) == {"preview", "pending_approval_id", "approval_url", "expires_at"}
    preview = body["preview"]
    assert preview["survivor_id"] == SURVIVOR
    assert preview["absorbed_id"] == ABSORBED
    assert preview["policy_case"] == 1
    assert preview["edges_to_move"] == {
        "requires": ["obl_1", "obl_2"],
        "covers": ["pa_1"],
        "mitigated_by": [],
    }
    assert preview["obligations_affected"] == 2
    assert body["approval_url"].startswith(
        f"http://unknown/approvals/{body['pending_approval_id']}#"
    )
    row = env.pending.get_by_id(body["pending_approval_id"])
    assert row is not None
    assert (row.actor_subject, row.actor_issuer) == (_OFFICER, _ISSUER)
    assert row.tool_name == "merge-capabilities"
    assert env.graph.write_calls == []


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({"survivor_id": SURVIVOR, "absorbed_id": SURVIVOR}, "itself"),
        ({"survivor_id": SURVIVOR, "absorbed_id": "cap_missing"}, "does not exist"),
    ],
)
def test_self_and_nonexistent_are_rejected_with_no_approval(
    env: _Env, args: dict[str, object], fragment: str
) -> None:
    text = env.call("merge-capabilities", args)

    assert text.startswith("error: ")
    assert fragment in text
    assert approval_row_count(env.pending) == 0
    assert env.graph.write_calls == []


def test_a_tombstone_is_rejected_with_no_approval(env: _Env) -> None:
    env.graph.nodes[1][2] = "merged"

    text = env.call("merge-capabilities", _MERGE_ARGS)

    assert text.startswith("error: ")
    assert "merged" in text
    assert approval_row_count(env.pending) == 0


def test_two_differently_governed_capabilities_are_rejected_naming_both_policies(
    env: _Env,
) -> None:
    env.graph.governors = [
        [ABSORBED, "pol_1", "Absorbed Policy", "draft"],
        [SURVIVOR, "pol_2", "Survivor Policy", "approved"],
    ]

    text = env.call("merge-capabilities", _MERGE_ARGS)

    assert text.startswith("error: ")
    for fragment in ("pol_1", "Absorbed Policy", "pol_2", "Survivor Policy"):
        assert fragment in text
    assert "release-capability-governance" in text
    assert approval_row_count(env.pending) == 0
    assert env.graph.write_calls == []


def test_two_approved_policies_are_rejected_with_the_honest_no_completion_path_text(
    env: _Env,
) -> None:
    env.graph.governors = [
        [ABSORBED, "pol_1", "A", "approved"],
        [SURVIVOR, "pol_2", "B", "approved"],
    ]

    text = env.call("merge-capabilities", _MERGE_ARGS)

    assert "no completion path" in text
    assert "fork carries the whole governed set" in text
    assert approval_row_count(env.pending) == 0


def test_the_same_policy_needs_no_acknowledgment_and_returns_an_approval(env: _Env) -> None:
    env.graph.governors = [
        [ABSORBED, "pol_1", "Incident Policy", "approved"],
        [SURVIVOR, "pol_1", "Incident Policy", "approved"],
    ]
    env.graph.governed_sets = [["pol_1", SURVIVOR], ["pol_1", ABSORBED]]

    body = json.loads(env.call("merge-capabilities", _MERGE_ARGS))

    assert set(body) == {"preview", "pending_approval_id", "approval_url", "expires_at"}
    assert body["preview"]["policy_case"] == 3
    assert body["preview"]["governance"]["acknowledgment_required"] is False
    row = env.pending.get_by_id(body["pending_approval_id"])
    assert row is not None
    assert row.normalized_args["acknowledge_governance_change"] is False
    assert env.graph.write_calls == []


def test_case_two_without_the_acknowledgment_returns_the_preview_and_no_approval(
    env: _Env,
) -> None:
    env.graph.governors = [[ABSORBED, "pol_1", "Incident Policy", "approved"]]

    body = json.loads(env.call("merge-capabilities", _MERGE_ARGS))

    assert set(body) == {"preview", "acknowledgment_required", "message"}
    assert body["acknowledgment_required"] is True
    governance = body["preview"]["governance"]
    assert body["preview"]["policy_case"] == 2
    assert governance["policy"] == {
        "id": "pol_1",
        "title": "Incident Policy",
        "status": "approved",
    }
    assert "obligations_coverage_changed" in governance
    assert "its governed set changes, its content/version does not" in body["message"]
    assert "acknowledge_governance_change=true" in body["message"]
    assert approval_row_count(env.pending) == 0
    assert env.graph.write_calls == []


def test_case_two_with_the_acknowledgment_creates_an_approval_signing_the_flag(env: _Env) -> None:
    env.graph.governors = [[ABSORBED, "pol_1", "Incident Policy", "approved"]]

    body = json.loads(
        env.call("merge-capabilities", {**_MERGE_ARGS, "acknowledge_governance_change": True})
    )

    assert set(body) == {"preview", "pending_approval_id", "approval_url", "expires_at"}
    assert body["preview"]["governance"]["acknowledgment_required"] is True
    row = env.pending.get_by_id(body["pending_approval_id"])
    assert row is not None
    assert row.normalized_args["acknowledge_governance_change"] is True
    assert env.graph.write_calls == []


def test_an_unreachable_graph_returns_the_sanitised_error(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    environment = _Env(monkeypatch, ScriptedMergeGraph())

    def _boom(_config: object) -> object:
        message = "connection refused to 10.0.0.1:6379"
        raise ConnectionError(message)

    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
        _factory(_boom),
    )

    text = environment.call("merge-capabilities", _MERGE_ARGS)

    assert text == "error: the policy graph database is not reachable"


def test_check_approval_reports_status_to_the_creator_only(env: _Env) -> None:
    created = json.loads(env.call("merge-capabilities", _MERGE_ARGS))
    approval_id = created["pending_approval_id"]

    mine = json.loads(env.call("check-cleanup-approval", {"pending_approval_id": approval_id}))
    theirs = env.call("check-cleanup-approval", {"pending_approval_id": approval_id}, sub=_OTHER)
    unknown = env.call("check-cleanup-approval", {"pending_approval_id": "nope"})

    assert mine == {
        "pending_approval_id": approval_id,
        "status": "pending",
        "tool_name": "merge-capabilities",
        "outcome": None,
    }
    assert theirs == f"error: no pending approval with id {approval_id!r}"
    assert unknown == "error: no pending approval with id 'nope'"


def test_the_acknowledgment_flag_is_part_of_the_signed_arguments(env: _Env) -> None:
    body = json.loads(
        env.call("merge-capabilities", {**_MERGE_ARGS, "acknowledge_governance_change": True})
    )

    row = env.pending.get_by_id(body["pending_approval_id"])
    assert row is not None
    assert row.normalized_args["acknowledge_governance_change"] is True
