"""The `create-policy-draft` MCP tool writes through the Graph Write Gateway.

Without a Capability claim a create is ONE group anchored to the `applied` audit row, which is
written first. The process gateway (the autouse rig) is the boundary; the audit store is the
event-id-reporting fake whose ids the rig's log accepts. Without a gateway the tool answers
`error:` and writes no audit row at all (fail closed).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

from api._gateway_fakes import installed_process_gateway
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors the other lifecycle tool tests
    FakeAccessRoleStore,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent
from policy_lifecycle._gateway_fakes import EventIdFakeAuditStore, PolicyReadGraph

from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server, tool_core

if TYPE_CHECKING:
    from collections.abc import Generator

    import pytest
    from graph_gateway._fakes import GatewayRig

_ACTOR = "policy-author"
_ISSUER = "https://issuer.example.com/"
_GRAPH = "policy_system"
_TITLE = "Data Protection Policy"
_STANDARDS: list[dict[str, object]] = [
    {"title": "Encryption", "controls": [{"title": "Rotate keys", "control_type": "automated"}]},
    {"title": "Backups"},
]


@contextlib.contextmanager
def _verified_actor(*, sub: str, iss: str = _ISSUER) -> Generator[None]:
    access_token = AccessToken(
        token="test-token", client_id="test-client", scopes=[], subject=sub, claims={"iss": iss}
    )
    token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(token)


class _Connection:
    def __init__(self, handle: PolicyReadGraph) -> None:
        self._handle = handle

    def select_graph(self, name: str) -> PolicyReadGraph:
        del name
        return self._handle


def _setup(monkeypatch: pytest.MonkeyPatch) -> tuple[GatewayRig, EventIdFakeAuditStore]:
    configure()
    rig = installed_process_gateway().rig
    graph = rig.graphs.open(_GRAPH)
    audit = EventIdFakeAuditStore(events=rig.events, event_id_sink=rig.store.audit_rows.add)
    handle = PolicyReadGraph(graph)

    def _connect(_config: object) -> _Connection:
        return _Connection(handle)

    def _audit_store(_config: object) -> EventIdFakeAuditStore:
        return audit

    def _access_role_store(_config: object, **_kwargs: object) -> FakeAccessRoleStore:
        return FakeAccessRoleStore()

    monkeypatch.setattr(tool_core, "connect_from_config", _connect)
    monkeypatch.setattr(tool_core, "PsycopgAuditStore", _audit_store)
    monkeypatch.setattr(tool_core, "PsycopgAccessRoleStore", _access_role_store)
    return rig, audit


def _create(arguments: dict[str, object] | None = None) -> CallToolResult:
    result = asyncio.run(
        mcp_server.server.call_tool("create-policy-draft", arguments or {"title": _TITLE})
    )
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_a_created_policy_is_one_group_linked_to_the_applied_row_and_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create with standards writes one group linked to the `applied` row and reads back."""
    rig, audit = _setup(monkeypatch)

    with _verified_actor(sub=_ACTOR):
        created = _create({"title": _TITLE, "standards": _STANDARDS})
        policy_id = json.loads(_text(created))["policy_id"]
        read = asyncio.run(mcp_server.server.call_tool("get-policy", {"policy_id": policy_id}))

    body = json.loads(_text(created))
    assert body["status"] == "draft"
    assert len(body["standard_ids"]) == 2
    (group,) = rig.store.groups
    assert rig.store.audit_link_of(group) == audit.event_ids[0]
    assert [row.outcome for row in audit.rows] == ["applied"]
    assert isinstance(read, CallToolResult)
    assert len(json.loads(_text(read))["standards"]) == 2


def test_a_taken_title_is_a_named_error_and_writes_no_second_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig, audit = _setup(monkeypatch)

    with _verified_actor(sub=_ACTOR):
        _create()
        again = _create()

    assert _text(again).startswith("error: ")
    assert len(rig.store.groups) == 1
    assert [row.outcome for row in audit.rows] == ["applied", "rejected"]


def test_without_a_gateway_the_tool_fails_closed_with_no_audit_row_and_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not even a `rejected` row exists without a gateway."""
    rig, audit = _setup(monkeypatch)
    install_graph_write_gateway(None)

    with _verified_actor(sub=_ACTOR):
        result = _create({"title": _TITLE, "standards": _STANDARDS})

    assert _text(result).startswith("error: graph writes are unavailable")
    assert audit.rows == []
    assert rig.store.groups == []


def test_a_gateway_failure_is_the_prefixed_persistence_error_and_a_failed_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing append answers the prefixed error, records `failed`, leaves the graph empty."""
    rig, audit = _setup(monkeypatch)
    rig.store.fail_appends()

    with _verified_actor(sub=_ACTOR):
        result = _create({"title": _TITLE, "standards": _STANDARDS})

    assert _text(result).startswith("error: FalkorDB write failed: ")
    assert [row.outcome for row in audit.rows] == ["applied", "failed"]
    assert rig.graphs.open(_GRAPH).nodes == {}
