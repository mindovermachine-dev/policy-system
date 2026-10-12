"""The Standard and Control draft tools write through the Graph Write Gateway (issue #211, S4).

`update-standard-draft`, `add-standard-to-draft`, `add-control-to-draft` and `update-control-draft`
on the process gateway rig (the true boundary), read back through the lifecycle read double over the
same `policy_system` graph. Each write is one group with no audit link and no audit row (AC-BI-008);
a cleared property is absent afterwards (AC-BI-007); without a gateway the tool fails closed and
writes nothing; a gateway failure is `error: FalkorDB write failed: ...`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

import pytest
from api._gateway_fakes import installed_process_gateway
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors the other lifecycle tool tests
    FakeAccessRoleStore,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent
from policy_lifecycle._gateway_fakes import (
    PolicyGraphSeeder,
    PolicyReadGraph,
    SeedControl,
    SeedStandard,
)

from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.domain_mapper.identity import control_id, standard_id
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Generator

    from graph_gateway._fakes import GatewayRig, InMemoryGraph

    from ps_service.graph_gateway.models import GraphLogGroup

_ACTOR = "policy-author"
_ISSUER = "https://issuer.example.com/"
_POLICY = "pol_data_protection_policy_aaaaaa"
_STANDARD = "std_encryption_aaaaaa"
_CONTROL = "ctl_rotation_aaaaaa"
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
    def __init__(self, handle: PolicyReadGraph) -> None:
        self._handle = handle

    def select_graph(self, name: str) -> PolicyReadGraph:
        del name
        return self._handle


class _AuditStore:
    """Records every audit call: a draft edit or add must make none."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def record(self, *args: object, **kwargs: object) -> str:
        self.calls.append((args, kwargs))
        return "unused"

    def record_standalone(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def _setup(monkeypatch: pytest.MonkeyPatch) -> tuple[GatewayRig, InMemoryGraph, _AuditStore]:
    configure()
    rig = installed_process_gateway().rig
    graph = rig.graphs.open(_GRAPH)
    PolicyGraphSeeder(graph).seed_policy(
        _POLICY,
        owner=(_ACTOR, _ISSUER),
        standards=(
            SeedStandard(
                _STANDARD,
                "Encryption",
                extra={"description": "old", "procedure": "proc"},
                controls=(
                    SeedControl(
                        _CONTROL, "Rotation", extra={"description": "old", "evidence_ref": "ref"}
                    ),
                ),
            ),
        ),
        legacy_edges=False,
    )
    audit = _AuditStore()
    handle = PolicyReadGraph(graph)

    def _connect(_config: object) -> _Connection:
        return _Connection(handle)

    def _audit_store(_config: object) -> _AuditStore:
        return audit

    def _access_role_store(_config: object, **_kwargs: object) -> FakeAccessRoleStore:
        return FakeAccessRoleStore()

    monkeypatch.setattr(mcp_server, "connect_from_config", _connect)
    monkeypatch.setattr(mcp_server, "PsycopgAuditStore", _audit_store)
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _access_role_store)
    return rig, graph, audit


def _call(tool: str, arguments: dict[str, object]) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool(tool, arguments))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _ops(group: GraphLogGroup) -> list[str]:
    return [str(entry.content["op"]) for entry in group.entries]


_WRITES: list[tuple[str, dict[str, object]]] = [
    ("update-standard-draft", {"standard_id": _STANDARD, "fields": {"description": "new"}}),
    ("update-control-draft", {"control_id": _CONTROL, "fields": {"description": "new"}}),
    ("add-standard-to-draft", {"policy_id": _POLICY, "title": "Backups"}),
    ("add-control-to-draft", {"standard_id": _STANDARD, "title": "Restore test"}),
]
_IDS = [tool for tool, _ in _WRITES]


def test_update_standard_draft_answers_the_same_shape_and_clears_a_property_in_one_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-007, AC-BI-008."""
    rig, graph, audit = _setup(monkeypatch)

    with _verified_actor(sub=_ACTOR):
        result = _call(
            "update-standard-draft",
            {"standard_id": _STANDARD, "fields": {"description": "new", "procedure": None}},
        )

    assert result.is_error is False
    assert json.loads(_text(result)) == {
        "standard_id": _STANDARD,
        "policy_id": _POLICY,
        "title": "Encryption",
        "status": "draft",
    }
    (group,) = rig.store.groups
    assert _ops(group) == ["merge_property", "remove_property"]
    assert rig.store.audit_link_of(group) is None
    assert audit.calls == []
    assert "procedure" not in graph.nodes[("Standard", _STANDARD)]


def test_update_control_draft_answers_the_same_shape_and_clears_a_property_in_one_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-007, AC-BI-008."""
    rig, graph, audit = _setup(monkeypatch)

    with _verified_actor(sub=_ACTOR):
        result = _call(
            "update-control-draft",
            {"control_id": _CONTROL, "fields": {"description": "new", "evidence_ref": None}},
        )

    assert result.is_error is False
    assert json.loads(_text(result)) == {
        "control_id": _CONTROL,
        "standard_id": _STANDARD,
        "policy_id": _POLICY,
        "title": "Rotation",
        "status": "draft",
    }
    (group,) = rig.store.groups
    assert _ops(group) == ["merge_property", "remove_property"]
    assert rig.store.audit_link_of(group) is None
    assert audit.calls == []
    assert "evidence_ref" not in graph.nodes[("Control", _CONTROL)]


def test_add_standard_to_draft_writes_the_node_and_edge_in_one_group_without_an_audit_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-008, AC-BI-009."""
    rig, graph, audit = _setup(monkeypatch)

    with _verified_actor(sub=_ACTOR):
        result = _call("add-standard-to-draft", {"policy_id": _POLICY, "title": "Backups"})

    new_id = standard_id(_POLICY, "Backups")
    assert json.loads(_text(result)) == {
        "standard_id": new_id,
        "policy_id": _POLICY,
        "title": "Backups",
        "status": "draft",
    }
    (group,) = rig.store.groups
    assert _ops(group) == ["upsert_node", "upsert_edge"]
    assert rig.store.audit_link_of(group) is None
    assert audit.calls == []
    assert graph.nodes[("Standard", new_id)]["status"] == "draft"


def test_add_control_to_draft_writes_the_node_and_edge_in_one_group_without_an_audit_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-008, AC-BI-009."""
    rig, graph, audit = _setup(monkeypatch)

    with _verified_actor(sub=_ACTOR):
        result = _call(
            "add-control-to-draft",
            {"standard_id": _STANDARD, "title": "Restore test", "control_type": "automated"},
        )

    new_id = control_id(_STANDARD, "Restore test")
    assert json.loads(_text(result))["control_id"] == new_id
    (group,) = rig.store.groups
    assert _ops(group) == ["upsert_node", "upsert_edge"]
    assert rig.store.audit_link_of(group) is None
    assert audit.calls == []
    assert graph.nodes[("Control", new_id)]["type"] == "automated"


def test_adding_the_same_standard_twice_logs_one_group_and_the_second_answer_is_the_same(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-009."""
    rig, _, _ = _setup(monkeypatch)

    with _verified_actor(sub=_ACTOR):
        first = _call("add-standard-to-draft", {"policy_id": _POLICY, "title": "Backups"})
        second = _call("add-standard-to-draft", {"policy_id": _POLICY, "title": "Backups"})

    assert _text(first) == _text(second)
    assert len(rig.store.groups) == 1


@pytest.mark.parametrize(("tool", "arguments"), _WRITES, ids=_IDS)
def test_without_a_gateway_the_tool_fails_closed_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, object]
) -> None:
    rig, graph, audit = _setup(monkeypatch)
    before = dict(graph.nodes)
    install_graph_write_gateway(None)

    with _verified_actor(sub=_ACTOR):
        result = _call(tool, arguments)

    assert _text(result).startswith("error: graph writes are unavailable")
    assert rig.store.groups == []
    assert graph.nodes == before
    assert audit.calls == []


@pytest.mark.parametrize(("tool", "arguments"), _WRITES, ids=_IDS)
def test_a_gateway_failure_is_the_prefixed_persistence_error_in_the_tool_answer(
    monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, object]
) -> None:
    rig, graph, _ = _setup(monkeypatch)
    before = dict(graph.nodes)
    rig.store.fail_appends()

    with _verified_actor(sub=_ACTOR):
        result = _call(tool, arguments)

    assert _text(result).startswith("error: FalkorDB write failed: ")
    assert graph.nodes == before


@pytest.mark.parametrize(("tool", "arguments"), _WRITES, ids=_IDS)
def test_a_non_owner_is_refused_before_anything_is_written(
    monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, object]
) -> None:
    rig, _, _ = _setup(monkeypatch)

    with _verified_actor(sub="someone-else"):
        result = _call(tool, arguments)

    assert _text(result) == "error: you do not have access to this Policy"
    assert rig.store.groups == []
