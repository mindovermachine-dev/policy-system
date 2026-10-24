# pyright: reportPrivateUsage=false
"""Cross-cutting sweeps over every graph-cleanup MCP tool.

Seven tools per the Container Architecture; each must be behind the authz-gate sweep
, return only sanitised errors, and every audit action the executors
write must be filterable through `list-audit-events`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from authz._fakes import FakeAccessRoleStore
from graph_cleanup._fakes import FakeApprovalStore, RecordingAuditStore
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from mcp_interface.test_access_role_tools import (
    _override,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
    _server,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
)
from mcp_interface.test_graph_cleanup_authz_gate import (
    _TOOLS as GATE_SWEEP,
)
from ps_service.audit.models import AuditEventRow, resolve_details_model
from ps_service.authz.models import AccessRole
from ps_service.graph_cleanup import audit_actions
from ps_service.logging import configure
from ps_service.mcp_interface.mcp_server import TOOL_GATES
from ps_service.mcp_interface.tool_gates import BypassPolicy, GateKind

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

CLEANUP_TOOLS = frozenset(
    {
        "find-capability-merge-candidates",
        "find-duplicate-obligations",
        "merge-capabilities",
        "merge-obligations",
        "merge-roles",
        "release-capability-governance",
        "unmerge",
        "check-cleanup-approval",
    }
)
CLEANUP_ACTIONS = (
    audit_actions.CAPABILITY_MERGE_ACTION,
    audit_actions.OBLIGATION_MERGE_ACTION,
    audit_actions.ROLE_MERGE_ACTION,
    audit_actions.ROLE_UNMERGE_ACTION,
    audit_actions.CAPABILITY_RELEASE_GOVERNANCE_ACTION,
    audit_actions.CAPABILITY_UNMERGE_ACTION,
    audit_actions.OBLIGATION_UNMERGE_ACTION,
)
_OWNER = "owner"
_OFFICER = "officer"
_ISSUER = "https://issuer.example.com/"
_LEAKS = ("10.0.0.1", "6379", "refused", "relation")


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


def _call(name: str, args: dict[str, object], *, sub: str) -> str:
    with _actor(sub):
        result = asyncio.run(_server().call_tool(name, args))
    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _roles() -> FakeAccessRoleStore:
    store = FakeAccessRoleStore(expected_owner=(_OWNER, _ISSUER))
    store.bootstrap_first_owner((_OWNER, _ISSUER))
    store.grant(
        actor=(_OWNER, _ISSUER),
        target=(_OFFICER, _ISSUER),
        access_role=AccessRole.COMPLIANCE_OFFICER,
    )
    return store


def test_exactly_the_eight_documented_tools_sit_behind_the_cleanup_gate() -> None:
    gated = {
        name
        for name, gate in TOOL_GATES.items()
        if gate.kind is GateKind.ROLE
        and gate.role is AccessRole.COMPLIANCE_OFFICER
        and gate.bypass is BypassPolicy.REFUSE
    }

    assert gated == CLEANUP_TOOLS
    assert len(gated) == 8


def test_the_authz_gate_sweep_covers_every_cleanup_tool() -> None:
    assert {name for name, _args in GATE_SWEEP} == CLEANUP_TOOLS


@pytest.mark.parametrize("name", sorted(CLEANUP_TOOLS))
def test_an_unreachable_graph_never_leaks_internals_from_any_tool(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    configure()

    def _boom(_config: object) -> object:
        message = "connection refused to 10.0.0.1:6379 relation x"
        raise ConnectionError(message)

    _override(
        monkeypatch,
        role_store_factory=_factory(_roles()),
        pending_approval_store_factory=_factory(FakeApprovalStore()),
        audit_store_factory=_factory(RecordingAuditStore()),
        graph_factory=_boom,
    )
    args: dict[str, object] = {
        "find-capability-merge-candidates": {},
        "find-duplicate-obligations": {},
        "merge-capabilities": {"survivor_id": "cap_a", "absorbed_id": "cap_b"},
        "merge-obligations": {"survivor_id": "obl_a", "absorbed_id": "obl_b"},
        "merge-roles": {"survivor_id": "role_a", "absorbed_id": "role_b"},
        "release-capability-governance": {"capability_id": "cap_a"},
        "unmerge": {"merged_id": "cap_b"},
        "check-cleanup-approval": {"pending_approval_id": "unknown"},
    }[name]  # pyright: ignore[reportAssignmentType]  -- values are the per-tool argument dicts

    text = _call(name, args, sub=_OFFICER)

    assert text.startswith("error: ")
    assert not any(leak in text for leak in _LEAKS)


def test_every_cleanup_action_is_registered_with_a_typed_details_model() -> None:
    for action in CLEANUP_ACTIONS:
        assert resolve_details_model(action) is not None, action


@pytest.mark.parametrize("action", CLEANUP_ACTIONS)
def test_list_audit_events_filtered_by_each_cleanup_action_returns_its_rows(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    configure()
    history = [
        AuditEventRow(
            id=f"00000000-0000-0000-0000-00000000000{index}",
            occurred_at=datetime(2026, 10, 5, tzinfo=UTC),
            actor_subject=_OFFICER,
            actor_issuer=_ISSUER,
            action=each,
            resource_type=each.split(".", maxsplit=1)[0],
            resource_id=f"res_{index}",
            outcome="applied",
            details={"approval_id": f"approval-{index}"},
        )
        for index, each in enumerate(CLEANUP_ACTIONS)
    ]
    _override(
        monkeypatch,
        role_store_factory=_factory(_roles()),
        audit_store_factory=_factory(RecordingAuditStore(history=history)),
    )

    body = json.loads(_call("list-audit-events", {"action": action}, sub=_OWNER))

    assert [event["action"] for event in body["events"]] == [action]
    assert body["events"][0]["details"] == {
        "approval_id": f"approval-{CLEANUP_ACTIONS.index(action)}"
    }


@pytest.mark.parametrize("resource_type", ["capability", "obligation", "role"])
def test_the_new_resource_types_are_accepted_as_audit_filters(
    monkeypatch: pytest.MonkeyPatch, resource_type: str
) -> None:
    configure()
    _override(
        monkeypatch,
        role_store_factory=_factory(_roles()),
        audit_store_factory=_factory(RecordingAuditStore()),
    )

    body = json.loads(_call("list-audit-events", {"resource_type": resource_type}, sub=_OWNER))

    assert body == {"events": [], "next_cursor": None}
