"""`approve-policy` / `reject-policy` audit a `PolicyManager` denial without changing their order.

Both tools declare a body gate: the role check stays where it always was, after the Policy read, so
an unknown id is still reported first and the response strings are unchanged. A denial for a
known Policy now writes one `access.gate` row; an unknown id, a permitted call and a call with no
real session write none. Under the local-test bypass the role is checked for the lifecycle
fallback identity but the row is attributed to the bypass sentinel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from authz._fakes import FakeAccessRoleStore

from mcp_interface.test_policy_lifecycle_tools import (
    _MANAGER_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _OTHER_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _approvable_fixture,  # pyright: ignore[reportPrivateUsage]  -- same fixtures as the lifecycle tool tests
    _bypass_owned_proposed_fixture,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _call_approve_policy,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _call_reject_policy,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _FakeAuditStore,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _install_access_role_store,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _install_audit_store,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _install_fixture_rig,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _install_manager_role,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _text,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _verified_actor,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.audit.actor import LOCAL_TEST_BYPASS_AUDIT_ACTOR
from ps_service.authz.models import AccessRole
from ps_service.logging import configure
from ps_service.mcp_interface.mcp_server import TOOL_GATES
from ps_service.mcp_interface.tool_gates import BypassPolicy, GateKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.types import CallToolResult

_DENIED = "error: You do not have the required access role for this action."
_TOOLS = ("approve-policy", "reject-policy")
_CALLS: dict[str, Callable[..., CallToolResult]] = {
    "approve-policy": _call_approve_policy,
    "reject-policy": _call_reject_policy,
}


@pytest.mark.parametrize("tool", _TOOLS)
def test_policy_transition_tool_declares_a_body_gate_for_policy_manager(tool: str) -> None:
    gate = TOOL_GATES[tool]
    assert (gate.kind, gate.role, gate.bypass) == (
        GateKind.BODY,
        AccessRole.POLICY_MANAGER,
        BypassPolicy.LIFECYCLE_IDENTITY,
    )


@pytest.mark.parametrize("tool", _TOOLS)
def test_denied_policy_transition_for_a_known_policy_writes_one_access_gate_row(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure()
    _install_fixture_rig(monkeypatch, _approvable_fixture())
    audit = _FakeAuditStore()
    _install_audit_store(monkeypatch, audit)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _CALLS[tool]()

    assert _text(result) == _DENIED
    (call,) = audit.calls
    assert (call.actor_subject, call.action, call.resource_id, call.outcome) == (
        _OTHER_SUBJECT,
        "access.gate",
        tool,
        "rejected",
    )
    assert dict(call.details) == {"required_role": "PolicyManager"}


@pytest.mark.parametrize("tool", _TOOLS)
def test_unknown_policy_id_is_still_reported_before_access_denied_for_an_unauthorised_caller(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure()
    _install_fixture_rig(monkeypatch, None)
    audit = _FakeAuditStore()
    _install_audit_store(monkeypatch, audit)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _CALLS[tool](policy_id="pol_missing")

    assert "no Policy exists" in _text(result)
    assert audit.calls == []


@pytest.mark.parametrize("tool", _TOOLS)
def test_permitted_policy_transition_writes_no_access_gate_row(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure()
    _install_fixture_rig(monkeypatch, _approvable_fixture())
    audit = _FakeAuditStore()
    _install_audit_store(monkeypatch, audit)
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _CALLS[tool]()

    assert not _text(result).startswith("error: ")
    assert [c.action for c in audit.calls if c.action == "access.gate"] == []


@pytest.mark.parametrize("tool", _TOOLS)
def test_policy_transition_without_a_real_session_writes_no_access_gate_row(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure()
    audit = _FakeAuditStore()
    _install_audit_store(monkeypatch, audit)

    result = _CALLS[tool]()

    assert _text(result).startswith("error: this action requires a real authenticated caller")
    assert audit.calls == []


@pytest.mark.parametrize("tool", _TOOLS)
def test_policy_transition_under_bypass_denial_row_actor_is_the_bypass_sentinel(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _install_fixture_rig(monkeypatch, _bypass_owned_proposed_fixture())
    audit = _FakeAuditStore()
    _install_audit_store(monkeypatch, audit)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    result = _CALLS[tool]()

    assert _text(result) == _DENIED
    (call,) = audit.calls
    assert (call.actor_subject, call.actor_issuer) == (
        LOCAL_TEST_BYPASS_AUDIT_ACTOR,
        LOCAL_TEST_BYPASS_AUDIT_ACTOR,
    )
    assert call.action == "access.gate"
