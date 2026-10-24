"""Structure checks for the policy draft tool group."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from ps_service.mcp_interface import mcp_server

_MODULE_DIR = Path(mcp_server.__file__).parent

_DRAFT_TOOLS = frozenset(
    {
        "create-policy-draft",
        "update-policy-draft",
        "add-standard-to-draft",
        "update-standard-draft",
        "add-control-to-draft",
        "update-control-draft",
    }
)


def test_draft_group_is_registered_first_among_the_policy_groups_and_declares_six_tools() -> None:
    assert importlib.util.find_spec("ps_service.mcp_interface.policy_draft_tools") is not None
    from ps_service.mcp_interface import policy_draft_tools, policy_transition_tools

    registered = [module for module, _ in mcp_server.TOOL_GROUP_MODULES]
    assert registered.index(policy_draft_tools) < registered.index(policy_transition_tools)
    built = mcp_server.build_server(
        mcp_server.build_production_environment(),
        groups=((policy_draft_tools, policy_draft_tools.register_policy_draft_tools),),
    )
    assert set(built.gates) == _DRAFT_TOOLS


def test_only_the_draft_and_transition_modules_serve_the_lifecycle_tools() -> None:
    policy_groups = {
        module.__name__.rsplit(".", 1)[-1]
        for module, _ in mcp_server.TOOL_GROUP_MODULES
        if module.__name__.rsplit(".", 1)[-1].startswith("policy_")
    }
    assert policy_groups == {"policy_draft_tools", "policy_transition_tools"}
    assert not any(
        name.startswith("policy_lifecycle")
        for name in (path.stem for path in _MODULE_DIR.glob("*.py"))
    )
