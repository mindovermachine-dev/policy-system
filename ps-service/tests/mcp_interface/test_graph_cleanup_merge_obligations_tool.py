"""`merge-obligations` (preview + approval) through the MCP tool (issue #190, slice 11).

AC-BI-005 (preview, no writes, approval bound to the pair), AC-BI-015 (cross-role rejected, no
approval), AC-BI-016 (self / nonexistent rejected, no approval), AC-BI-018 (sanitised errors).
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
    OBL_ABSORBED,
    OBL_SURVIVOR,
    FakeApprovalStore,
    ScriptedObligationGraph,
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


def _opener_for(graph: ScriptedObligationGraph) -> Callable[[object], ScriptedObligationGraph]:
    def _open(_config: object) -> ScriptedObligationGraph:
        return graph

    return _open


class _Env:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, graph: ScriptedObligationGraph) -> None:
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
    return _Env(monkeypatch, ScriptedObligationGraph())


_MERGE_ARGS: dict[str, object] = {"survivor_id": OBL_SURVIVOR, "absorbed_id": OBL_ABSORBED}


def test_merge_returns_the_preview_and_a_pending_approval_and_writes_nothing(env: _Env) -> None:
    body = json.loads(env.call("merge-obligations", _MERGE_ARGS))

    assert set(body) == {"preview", "pending_approval_id", "approval_url", "expires_at"}
    preview = body["preview"]
    assert preview["survivor_id"] == OBL_SURVIVOR
    assert preview["absorbed_id"] == OBL_ABSORBED
    assert preview["role_id"] == "role_1"
    assert preview["role_name"] == "Manufacturer"
    assert preview["edges_to_move"] == {"satisfied_by": ["req_1", "req_2"], "requires": ["cap_1"]}
    assert preview["duplicate_edges_collapsed"] == 2
    assert preview["requirement_source_refs"] == [
        {"requirement_id": "req_1", "source_ref": "Art. 6(1)"},
        {"requirement_id": "req_2", "source_ref": "Art. 6(2)"},
    ]
    assert body["approval_url"].startswith(
        f"http://unknown/approvals/{body['pending_approval_id']}#"
    )
    row = env.pending.get_by_id(body["pending_approval_id"])
    assert row is not None
    assert (row.actor_subject, row.actor_issuer) == (_OFFICER, _ISSUER)
    assert row.tool_name == "merge-obligations"
    assert row.normalized_args == {
        "survivor_id": OBL_SURVIVOR,
        "absorbed_id": OBL_ABSORBED,
        "state_digest": preview["state_digest"],
    }
    assert env.graph.write_calls == []


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({"survivor_id": OBL_SURVIVOR, "absorbed_id": OBL_SURVIVOR}, "itself"),
        ({"survivor_id": OBL_SURVIVOR, "absorbed_id": "obl_missing"}, "does not exist"),
    ],
)
def test_self_and_nonexistent_are_rejected_with_no_approval(
    env: _Env, args: dict[str, object], fragment: str
) -> None:
    text = env.call("merge-obligations", args)

    assert text.startswith("error: ")
    assert fragment in text
    assert approval_row_count(env.pending) == 0
    assert env.graph.write_calls == []


def test_obligations_under_different_roles_are_rejected_with_no_approval(env: _Env) -> None:
    env.graph.roles[1] = [OBL_ABSORBED, "role_2", "Importer"]

    text = env.call("merge-obligations", _MERGE_ARGS)

    assert text.startswith("error: obligations under different roles cannot be merged")
    assert "Importer" in text
    assert approval_row_count(env.pending) == 0
    assert env.graph.write_calls == []


def test_an_unreachable_graph_returns_the_sanitised_error(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    environment = _Env(monkeypatch, ScriptedObligationGraph())

    def _boom(_config: object) -> object:
        message = "connection refused to 10.0.0.1:6379"
        raise ConnectionError(message)

    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
        _factory(_boom),
    )

    text = environment.call("merge-obligations", _MERGE_ARGS)

    assert text == "error: the policy graph database is not reachable"


def test_check_approval_reports_an_obligation_approval_to_its_creator_only(env: _Env) -> None:
    created = json.loads(env.call("merge-obligations", _MERGE_ARGS))
    approval_id = created["pending_approval_id"]

    mine = json.loads(env.call("check-cleanup-approval", {"pending_approval_id": approval_id}))
    theirs = env.call("check-cleanup-approval", {"pending_approval_id": approval_id}, sub=_OTHER)

    assert mine == {
        "pending_approval_id": approval_id,
        "status": "pending",
        "tool_name": "merge-obligations",
        "outcome": None,
    }
    assert theirs == f"error: no pending approval with id {approval_id!r}"
