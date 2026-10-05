"""`unmerge` (preview + approval) through the MCP tool, capability merges (issue #190, slice 15).

AC-BI-019 (preview lists restored and survivor-added edges, approval bound to the merge and the
state, no graph write), AC-BI-020 (conflicts explained, no approval), AC-BI-018 (sanitised errors).
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
    ABSORBED,
    SURVIVOR,
    FakeApprovalStore,
    RecordingAuditStore,
    ScriptedUnmergeGraph,
    approval_row_count,
    capability_merge_history,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from ps_service.audit.errors import AuditPostgresUnavailableError
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


def _opener_for(graph: ScriptedUnmergeGraph) -> Callable[[object], ScriptedUnmergeGraph]:
    def _open(_config: object) -> ScriptedUnmergeGraph:
        return graph

    return _open


class _Env:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, graph: ScriptedUnmergeGraph) -> None:
        self.graph = graph
        self.pending = FakeApprovalStore()
        self.audit = RecordingAuditStore(history=capability_merge_history())
        monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _factory(_officers()))
        monkeypatch.setattr(mcp_server, "PsycopgPendingApprovalStore", _factory(self.pending))
        monkeypatch.setattr(
            "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
            _factory(_opener_for(graph)),
        )
        monkeypatch.setattr(
            "ps_service.graph_cleanup.dependencies.PsycopgAuditStore", _factory(self.audit)
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
    return _Env(monkeypatch, ScriptedUnmergeGraph())


_ARGS: dict[str, object] = {"merged_id": ABSORBED}


def test_a_merged_capability_returns_the_preview_and_a_pending_approval_and_writes_nothing(
    env: _Env,
) -> None:
    env.graph.requires = [*env.graph.requires, ["obl_new", SURVIVOR]]

    body = json.loads(env.call("unmerge", _ARGS))

    assert set(body) == {"preview", "pending_approval_id", "approval_url", "expires_at"}
    preview = body["preview"]
    assert preview["kind"] == "capability"
    assert (preview["merged_id"], preview["survivor_id"]) == (ABSORBED, SURVIVOR)
    assert preview["merge_approval_id"] == "merge-approval-1"
    assert preview["edges_to_restore"]["requires"] == ["obl_1", "obl_2"]
    assert preview["edges_removed_from_survivor"]["requires"] == ["obl_1"]
    assert [e["source_id"] for e in preview["survivor_added_edges"]] == ["obl_new"]
    assert body["approval_url"].startswith(
        f"http://unknown/approvals/{body['pending_approval_id']}#"
    )
    row = env.pending.get_by_id(body["pending_approval_id"])
    assert row is not None
    assert (row.actor_subject, row.actor_issuer) == (_OFFICER, _ISSUER)
    assert row.tool_name == "unmerge"
    assert row.normalized_args == {
        "merged_id": ABSORBED,
        "merge_approval_id": "merge-approval-1",
        "kind": "capability",
        "state_digest": preview["state_digest"],
    }
    assert env.graph.write_calls == []
    assert env.audit.rows == []


def test_an_id_with_no_merge_in_the_audit_trail_is_rejected_with_no_approval(env: _Env) -> None:
    env.audit.history = []

    text = env.call("unmerge", _ARGS)

    assert text.startswith("error: ")
    assert "no merge" in text
    assert approval_row_count(env.pending) == 0


def test_a_conflict_is_rejected_with_its_explanation_and_no_approval(env: _Env) -> None:
    env.graph.nodes[0][2] = "merged"
    env.graph.redirects = [[ABSORBED, SURVIVOR], [SURVIVOR, "cap_winner"]]

    text = env.call("unmerge", _ARGS)

    assert text.startswith("error: ")
    assert "cap_winner" in text
    assert "no longer active" in text
    assert approval_row_count(env.pending) == 0
    assert env.graph.write_calls == []


def test_an_unreadable_audit_trail_returns_the_sanitised_error(env: _Env) -> None:
    env.audit.query_error = AuditPostgresUnavailableError("db host 10.1.2.3 down")

    text = env.call("unmerge", _ARGS)

    assert text == "error: the audit trail could not be read right now; try again shortly"
    assert approval_row_count(env.pending) == 0


def test_an_unreachable_graph_returns_the_sanitised_error(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    environment = _Env(monkeypatch, ScriptedUnmergeGraph())

    def _boom(_config: object) -> object:
        message = "connection refused to 10.0.0.1:6379"
        raise ConnectionError(message)

    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
        _factory(_boom),
    )

    text = environment.call("unmerge", _ARGS)

    assert text == "error: the policy graph database is not reachable"


def test_check_approval_reports_an_unmerge_approval_to_its_creator_only(env: _Env) -> None:
    created = json.loads(env.call("unmerge", _ARGS))
    approval_id = created["pending_approval_id"]

    mine = json.loads(env.call("check-cleanup-approval", {"pending_approval_id": approval_id}))
    theirs = env.call("check-cleanup-approval", {"pending_approval_id": approval_id}, sub=_OTHER)

    assert mine == {
        "pending_approval_id": approval_id,
        "status": "pending",
        "tool_name": "unmerge",
        "outcome": None,
    }
    assert theirs == f"error: no pending approval with id {approval_id!r}"
