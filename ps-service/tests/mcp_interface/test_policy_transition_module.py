"""Structure checks for the policy transition tool group and its shared core constants."""

from __future__ import annotations

from ps_service.authz.models import AccessRole
from ps_service.mcp_interface import mcp_server, policy_transition_tools, tool_core
from ps_service.mcp_interface.tool_gates import BypassPolicy, GateKind

_TRANSITION_TOOLS = frozenset(
    {"get-policy", "propose-policy", "approve-policy", "reject-policy", "revert-policy-to-draft"}
)


def test_core_exports_the_shared_policy_lifecycle_constants() -> None:
    assert "POLICY_TRANSITION_GATE" in tool_core.__all__
    assert "POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE" in tool_core.__all__


def test_core_transition_gate_is_body_gated_for_policy_managers() -> None:
    gate = tool_core.POLICY_TRANSITION_GATE
    assert gate.kind is GateKind.BODY
    assert gate.role is AccessRole.POLICY_MANAGER
    assert gate.bypass is BypassPolicy.LIFECYCLE_IDENTITY
    assert (
        gate.unauthenticated_message
        == tool_core.POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
    )


def test_transition_group_is_registered_and_declares_exactly_its_five_tools() -> None:
    registered = [module for module, _ in mcp_server.TOOL_GROUP_MODULES]
    assert policy_transition_tools in registered
    built = mcp_server.build_server(
        mcp_server.build_production_environment(),
        groups=(
            (policy_transition_tools, policy_transition_tools.register_policy_transition_tools),
        ),
    )
    assert set(built.gates) == _TRANSITION_TOOLS
