"""The `update-policy-draft` MCP tool writes through the Graph Write Gateway.

The vertical slice: MCP tool -> service -> gateway -> log -> read back. The process gateway (the
rig the package's autouse fixture installs) is the true boundary; the read handle is the lifecycle
read double over that rig's `policy_system` graph, so what the gateway wrote is what the next read
sees. A draft edit writes no audit row and its group carries no audit link; a cleared
property is absent afterwards; with no gateway the tool fails closed and writes
nothing.
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
from policy_lifecycle._gateway_fakes import PolicyGraphSeeder, PolicyReadGraph, SeedStandard

from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server, tool_core

if TYPE_CHECKING:
    from collections.abc import Generator

    import pytest
    from graph_gateway._fakes import GatewayRig, InMemoryGraph

    from ps_service.graph_gateway.models import GraphLogGroup

_ACTOR = "policy-author"
_ISSUER = "https://issuer.example.com/"
_POLICY = "pol_data_protection_policy_aaaaaa"
_GRAPH = "policy_system"


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
    """Stands in for the eager FalkorDB client: every `select_graph` answers the read double."""

    def __init__(self, handle: PolicyReadGraph) -> None:
        self._handle = handle

    def select_graph(self, name: str) -> PolicyReadGraph:
        del name
        return self._handle


class _AuditStore:
    """Records every audit call: a draft edit must make none."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def record(self, *args: object, **kwargs: object) -> str:
        self.calls.append((args, kwargs))
        return "unused"

    def record_standalone(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def _setup(
    monkeypatch: pytest.MonkeyPatch, *, legacy_edges: bool = False, **extra: object
) -> tuple[GatewayRig, InMemoryGraph, _AuditStore]:
    configure()
    rig = installed_process_gateway().rig
    graph = rig.graphs.open(_GRAPH)
    PolicyGraphSeeder(graph).seed_policy(
        _POLICY,
        owner=(_ACTOR, _ISSUER),
        extra=extra,
        standards=(SeedStandard("std_1", "Encryption"),),
        legacy_edges=legacy_edges,
    )
    audit = _AuditStore()
    handle = PolicyReadGraph(graph)

    def _connect(_config: object) -> _Connection:
        return _Connection(handle)

    def _audit_store(_config: object) -> _AuditStore:
        return audit

    def _access_role_store(_config: object, **_kwargs: object) -> FakeAccessRoleStore:
        return FakeAccessRoleStore()

    monkeypatch.setattr(tool_core, "connect_from_config", _connect)
    monkeypatch.setattr(tool_core, "PsycopgAuditStore", _audit_store)
    monkeypatch.setattr(tool_core, "PsycopgAccessRoleStore", _access_role_store)
    return rig, graph, audit


def _call(fields: dict[str, object] | None, *, policy_id: str = _POLICY) -> CallToolResult:
    arguments: dict[str, object] = {"policy_id": policy_id}
    if fields is not None:
        arguments["fields"] = fields
    result = asyncio.run(mcp_server.server.call_tool("update-policy-draft", arguments))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _ops(group: GraphLogGroup) -> list[str]:
    return [str(entry.content["op"]) for entry in group.entries]


def test_the_tool_answers_the_same_shape_and_logs_one_group_without_an_audit_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig, _, audit = _setup(monkeypatch, description="old", scope_out="out")

    with _verified_actor(sub=_ACTOR):
        result = _call({"description": "revised text", "scope_out": None})

    assert result.is_error is False
    assert json.loads(_text(result)) == {
        "policy_id": _POLICY,
        "updated_fields": ["description", "scope_out"],
    }
    (group,) = rig.store.groups
    assert _ops(group) == ["merge_property", "remove_property"]
    assert rig.store.audit_link_of(group) is None
    assert audit.calls == []  # a draft edit writes no audit row


def test_the_cleared_property_is_absent_afterwards_and_the_edit_is_visible_to_a_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleared property is absent, round-tripped through the tool and `get-policy`'s read."""
    _, graph, _ = _setup(monkeypatch, description="old", scope_out="out")

    with _verified_actor(sub=_ACTOR):
        update = _call({"description": "revised text", "scope_out": None})
        read = asyncio.run(mcp_server.server.call_tool("get-policy", {"policy_id": _POLICY}))

    node = graph.nodes[("Policy", _POLICY)]
    assert update.is_error is False
    assert "scope_out" not in node
    assert node["description"] == "revised text"
    assert isinstance(read, CallToolResult)
    assert json.loads(_text(read))["status"] == "draft"


def test_re_applying_the_same_patch_logs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    rig, _, _ = _setup(monkeypatch, description="old")

    with _verified_actor(sub=_ACTOR):
        _call({"description": "new"})
        logged = len(rig.store.groups)
        second = _call({"description": "new"})

    assert second.is_error is False
    assert len(rig.store.groups) == logged


def test_the_first_call_on_a_graph_with_legacy_edges_logs_a_stamp_group_with_no_link_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig, graph, _ = _setup(monkeypatch, legacy_edges=True, description="old")

    with _verified_actor(sub=_ACTOR):
        result = _call({"description": "new"})

    assert result.is_error is False
    stamp, edit = rig.store.groups
    assert (_ops(stamp), _ops(edit)) == (["stamp_edge_identity"], ["merge_property"])
    assert (rig.store.audit_link_of(stamp), rig.store.audit_link_of(edit)) == (None, None)
    assert (
        "SUPPORTED_BY",
        "Policy",
        _POLICY,
        "Standard",
        "std_1",
        f"SUPPORTED_BY:{_POLICY}->std_1",
    ) in (graph.edges)


def test_without_a_gateway_the_tool_fails_closed_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig, graph, audit = _setup(monkeypatch, description="old")
    install_graph_write_gateway(None)

    with _verified_actor(sub=_ACTOR):
        result = _call({"description": "new"})

    assert result.is_error is False
    assert _text(result).startswith("error: graph writes are unavailable")
    assert rig.store.groups == []
    assert graph.nodes[("Policy", _POLICY)]["description"] == "old"
    assert audit.calls == []


def test_a_gateway_failure_is_the_prefixed_persistence_error_in_the_tool_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig, graph, _ = _setup(monkeypatch, description="old")
    rig.store.fail_appends()

    with _verified_actor(sub=_ACTOR):
        result = _call({"description": "new"})

    assert result.is_error is False
    assert _text(result).startswith("error: FalkorDB write failed: ")
    assert graph.nodes[("Policy", _POLICY)]["description"] == "old"


def test_a_non_owner_is_refused_before_anything_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig, _, _ = _setup(monkeypatch, description="old")

    with _verified_actor(sub="someone-else"):
        result = _call({"description": "hijacked"})

    assert _text(result) == "error: you do not have access to this Policy"
    assert rig.store.groups == []
