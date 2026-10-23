"""Structural guards for the split of the MCP surface into one module per tool group.

The MCP surface is `tool_core.py` (the one `MCPServer`, the gates and the shared plumbing), one
`*_tools.py` module per tool group, and the thin `mcp_server.py` front door. These guards are
written against the files on disk, not against a fixed module list, so they hold on the unsplit
tree (where they are vacuous or trivially true) and bite as modules appear:

* every tool is registered in a fresh interpreter that imports only the package;
* the repository has exactly one `MCPServer(...)` construction;
* no tool-group module imports another tool-group module or the front door.

Each checker is itself checked against a planted violation, so a checker that silently passes
everything is caught.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import subprocess
import sys
import tomllib
import types
from pathlib import Path

import pytest

import ps_service.mcp_interface as mcp_package

_MI = Path(mcp_package.__file__).parent

_EXPECTED_TOOLS = frozenset(
    {
        "add-control-to-draft",
        "add-standard-to-draft",
        "approve-policy",
        "check-cleanup-approval",
        "check_instrument_ingestion_status",
        "check_regulations",
        "create-policy-draft",
        "cypher",
        "domain_concepts",
        "find-capability-merge-candidates",
        "find-duplicate-obligations",
        "get-catalog-listing",
        "get-catalog-source",
        "get-policy",
        "get_ingestion_status",
        "grant-access-role",
        "ingest_regulation",
        "invite-user",
        "list-access-roles",
        "list-audit-events",
        "merge-capabilities",
        "merge-obligations",
        "merge-roles",
        "near_misses_check_approval",
        "near_misses_list",
        "near_misses_resolve",
        "propose-policy",
        "reject-policy",
        "release-capability-governance",
        "reset-catalog-source",
        "restore_instrument",
        "revert-policy-to-draft",
        "revoke-access-role",
        "set-catalog-source",
        "start_ingestion",
        "unmerge",
        "update-control-draft",
        "update-policy-draft",
        "update-standard-draft",
        "verify-audit-log",
    }
)
_EXPECTED_RESOURCES = frozenset({"psdomain://concepts"})

# Run in a fresh interpreter: imports ONLY the package, then reads the registry of the one server.
_FRESH_IMPORT_PROBE = """
import asyncio, json, sys
import ps_service.mcp_interface
server = sys.modules["ps_service.mcp_interface.mcp_server"].server
tools = sorted(t.name for t in asyncio.run(server.list_tools()))
resources = sorted(str(r.uri) for r in asyncio.run(server.list_resources()))
print(json.dumps({"tools": tools, "resources": resources}))
"""


def test_importing_the_package_in_a_fresh_interpreter_registers_every_tool() -> None:
    completed = subprocess.run(  # noqa: S603 -- fixed argv: the running interpreter plus a constant probe
        [sys.executable, "-I", "-c", _FRESH_IMPORT_PROBE],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    registered = json.loads(completed.stdout.strip().splitlines()[-1])

    assert set(registered["tools"]) == _EXPECTED_TOOLS
    assert len(registered["tools"]) == len(_EXPECTED_TOOLS)
    assert set(registered["resources"]) == _EXPECTED_RESOURCES


def _count_server_constructions(tree: ast.AST) -> int:
    """Number of `MCPServer(...)` calls (by bare or dotted name) in `tree`."""
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            count += name == "MCPServer"
    return count


def test_exactly_one_mcpserver_is_constructed_across_the_package() -> None:
    constructions = {
        path.name: _count_server_constructions(ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(_MI.glob("*.py"))
    }

    assert sum(constructions.values()) == 1, constructions


def test_the_construction_counter_sees_a_planted_second_server() -> None:
    planted = "from mcp.server import MCPServer\nother = MCPServer(name='x')\n"

    assert _count_server_constructions(ast.parse(planted)) == 1
    assert _count_server_constructions(ast.parse("x = 1\n")) == 0


_NON_GROUP_PACKAGE_MODULES = frozenset(
    {"tool_core", "tool_gates", "errors", "audit_actions", "draft_input_models"}
)
_PACKAGE = "ps_service.mcp_interface"
_NONE: set[str] = set()


def _imported_package_modules(tree: ast.AST) -> set[str]:
    """Names of `ps_service.mcp_interface` submodules that `tree` imports, however spelled."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(f"{_PACKAGE}."):
                    found.add(alias.name.split(".")[2])
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == _PACKAGE:
                found.update(alias.name for alias in node.names)
            elif module.startswith(f"{_PACKAGE}."):
                found.add(module.split(".")[2])
    return found


def _group_imports_violating_independence(tree: ast.AST) -> set[str]:
    """Imported package modules that a tool-group module must not import."""
    return _imported_package_modules(tree) - _NON_GROUP_PACKAGE_MODULES


def test_no_tool_group_module_imports_another_group_or_the_front_door() -> None:
    violations = {
        path.name: sorted(
            _group_imports_violating_independence(ast.parse(path.read_text(encoding="utf-8")))
        )
        for path in sorted(_MI.glob("*_tools.py"))
    }

    assert {name: bad for name, bad in violations.items() if bad} == {}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from ps_service.mcp_interface import near_miss_tools\n", {"near_miss_tools"}),
        ("import ps_service.mcp_interface.mcp_server\n", {"mcp_server"}),
        ("from ps_service.mcp_interface.cypher_tools import x\n", {"cypher_tools"}),
        ("from ps_service.mcp_interface import tool_core\n", _NONE),
        ("from ps_service.mcp_interface.tool_gates import ToolGate\n", _NONE),
        ("from ps_service.mcp_interface.errors import McpGraphUnavailableError\n", _NONE),
        ("from ps_service.mcp_interface.audit_actions import QUERY_CYPHER_ACTION\n", _NONE),
        ("from ps_service.audit.emit import AuditContext\n", _NONE),
    ],
    ids=[
        "from_package_import_group",
        "import_front_door",
        "from_group_module",
        "tool_core_ok",
        "tool_gates_ok",
        "errors_ok",
        "audit_actions_ok",
        "unrelated_ok",
    ],
)
def test_the_independence_checker_flags_planted_violations_and_only_those(
    source: str, expected: set[str]
) -> None:
    assert _group_imports_violating_independence(ast.parse(source)) == expected


# Names the MCP layer must never hold: process control and the Query Engine's write-clause guard.
_FORBIDDEN_NAMES = (
    "subprocess",
    "sys",
    "max_rows",
    "row_limit",
    "is_write_clause",
    "_WRITE_CLAUSE",
    "_WRITE_CLAUSE_REJECTION_MESSAGE",
)


def _tool_surface_module_names() -> list[str]:
    """`mcp_server`, `tool_core` (once it exists) and every `*_tools` module, found by glob."""
    names = {"mcp_server"} | {p.stem for p in _MI.glob("tool_core.py")}
    return sorted(names | {p.stem for p in _MI.glob("*_tools.py")})


def _forbidden_names_held_by(module: types.ModuleType) -> list[str]:
    return [name for name in _FORBIDDEN_NAMES if hasattr(module, name)]


@pytest.mark.parametrize("name", _tool_surface_module_names())
def test_tool_modules_expose_no_forbidden_names(name: str) -> None:
    module = importlib.import_module(f"{_PACKAGE}.{name}")

    assert _forbidden_names_held_by(module) == []


def test_the_forbidden_name_check_flags_a_planted_module() -> None:
    planted = types.ModuleType("probe")
    planted.subprocess = subprocess  # pyright: ignore[reportAttributeAccessIssue]  -- planting the forbidden name

    assert _forbidden_names_held_by(planted) == ["subprocess"]
    assert _forbidden_names_held_by(types.ModuleType("clean")) == []


def test_the_forbidden_name_check_covers_every_tool_surface_module_on_disk() -> None:
    on_disk = {p.stem for p in _MI.glob("*_tools.py")} | {"mcp_server"}
    on_disk |= {p.stem for p in _MI.glob("tool_core.py")}

    assert set(_tool_surface_module_names()) == on_disk


# The patch seam: tests and deployments patch these four names on `tool_core`; every consumer
# resolves them there at call time, so one patch statement reaches every tool of every group.
_SEAM_CLASSES = frozenset(
    {"PsycopgAccessRoleStore", "PsycopgAuditStore", "PsycopgPendingApprovalStore"}
)
_SEAM_NAMES = _SEAM_CLASSES | {"connect_from_config"}


def _seam_violations(tree: ast.AST) -> list[str]:
    """How a module outside `tool_core` uses a seam name (only `tool_core.<Name>` is allowed)."""
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            problems.extend(
                f"imports {alias.name}" for alias in node.names if alias.name in _SEAM_NAMES
            )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _SEAM_NAMES:
                problems.append(f"calls bare {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in _SEAM_NAMES:
                owner = func.value.id if isinstance(func.value, ast.Name) else ""
                if owner != "tool_core":
                    problems.append(f"calls {owner or '?'}.{func.attr}")
    return problems


_NON_CORE_MODULES = sorted(path for path in _MI.glob("*.py") if path.name != "tool_core.py")


@pytest.mark.parametrize("path", _NON_CORE_MODULES, ids=[p.name for p in _NON_CORE_MODULES])
def test_only_tool_core_binds_the_patch_seam_names(path: Path) -> None:
    assert _seam_violations(ast.parse(path.read_text(encoding="utf-8"))) == []


def test_the_seam_ids_cover_every_module_but_tool_core() -> None:
    assert {p.name for p in _NON_CORE_MODULES} == {p.name for p in _MI.glob("*.py")} - {
        "tool_core.py"
    }


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from ps_service.audit.store import PsycopgAuditStore\n", ["imports PsycopgAuditStore"]),
        ("def f(c):\n    return PsycopgAuditStore(c)\n", ["calls bare PsycopgAuditStore"]),
        ("def f(c):\n    return connect_from_config(c)\n", ["calls bare connect_from_config"]),
        (
            "def f(c):\n    return other.PsycopgAccessRoleStore(c)\n",
            ["calls other.PsycopgAccessRoleStore"],
        ),
        ("def f(c):\n    return tool_core.PsycopgAuditStore(c)\n", []),
        ("def f(c):\n    return tool_core.connect_from_config(c)\n", []),
        ("from ps_service.mcp_interface import tool_core\n", []),
    ],
    ids=[
        "import",
        "bare_class",
        "bare_connect",
        "foreign_owner",
        "seam_ok",
        "connect_ok",
        "module_ok",
    ],
)
def test_the_seam_checker_flags_planted_violations_and_only_those(
    source: str, expected: list[str]
) -> None:
    assert _seam_violations(ast.parse(source)) == expected


def test_ruff_runtime_evaluated_decorator_names_the_real_tool_registration_decorator() -> None:
    """The MCP SDK resolves tool signatures at runtime; ruff must know the decorator that does."""
    pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    decorators = config["tool"]["ruff"]["lint"]["flake8-type-checking"][
        "runtime-evaluated-decorators"
    ]

    assert decorators == [f"{_PACKAGE}.tool_core.gated_server_tool"]
    core = importlib.import_module(f"{_PACKAGE}.tool_core")
    assert callable(core.gated_server_tool)


_CLEANUP_TOOL_NAMES = frozenset(
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


def _gated_tool_names_in(path: Path) -> set[str]:
    """Tool names registered by `@_gated_tool("<name>", ...)` at the top level of `path`."""
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Name)
                and decorator.func.id == "_gated_tool"
                and isinstance(decorator.args[0], ast.Constant)
            ):
                names.add(str(decorator.args[0].value))
    return names


def test_every_graph_cleanup_tool_is_registered_in_the_graph_cleanup_tools_module() -> None:
    module = _MI / "graph_cleanup_tools.py"

    assert module.is_file()
    assert _gated_tool_names_in(module) == _CLEANUP_TOOL_NAMES


_FRONT_DOOR = _MI / "mcp_server.py"
_MAX_MODULE_LINES = 1500

# Pre-split `tools/list` order (clients see registration order), with one deliberate move:
# `check_instrument_ingestion_status` is registered by `ingestion_tools` (it is read together with
# `check_regulations`), so it follows that tool instead of `restore_instrument`.
_EXPECTED_TOOL_ORDER = [
    "ingest_regulation",
    "start_ingestion",
    "get_ingestion_status",
    "check_regulations",
    "check_instrument_ingestion_status",
    "near_misses_list",
    "near_misses_resolve",
    "near_misses_check_approval",
    "set-catalog-source",
    "reset-catalog-source",
    "get-catalog-source",
    "invite-user",
    "get-catalog-listing",
    "restore_instrument",
    "cypher",
    "domain_concepts",
    "list-access-roles",
    "grant-access-role",
    "revoke-access-role",
    "list-audit-events",
    "verify-audit-log",
    "create-policy-draft",
    "update-policy-draft",
    "add-standard-to-draft",
    "update-standard-draft",
    "add-control-to-draft",
    "update-control-draft",
    "get-policy",
    "propose-policy",
    "approve-policy",
    "reject-policy",
    "revert-policy-to-draft",
    "find-capability-merge-candidates",
    "find-duplicate-obligations",
    "merge-capabilities",
    "merge-obligations",
    "merge-roles",
    "release-capability-governance",
    "unmerge",
    "check-cleanup-approval",
]

_TOOL_REGISTRATION_ATTRIBUTES = frozenset({"tool", "resource"})


def _front_door_violations(tree: ast.AST) -> list[str]:
    """Definitions and registrations that must not appear in the front door."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.append(f"definition {node.name}")
        elif isinstance(node, ast.Name) and node.id in {"_gated_tool", "gated_server_tool"}:
            found.append(f"registration {node.id}")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in _TOOL_REGISTRATION_ATTRIBUTES
            and isinstance(node.value, ast.Name)
            and node.value.id == "server"
        ):
            found.append(f"registration server.{node.attr}")
    return found


def test_the_front_door_defines_no_tool_and_exports_server_and_handle_mcp_tool_call() -> None:
    front_door = importlib.import_module(f"{_PACKAGE}.mcp_server")

    assert _front_door_violations(ast.parse(_FRONT_DOOR.read_text(encoding="utf-8"))) == []
    assert {"server", "handle_mcp_tool_call"} <= set(front_door.__all__)
    assert front_door.server is importlib.import_module(f"{_PACKAGE}.tool_core").server
    assert callable(front_door.handle_mcp_tool_call)


def test_the_front_door_checker_flags_planted_definitions_and_registrations() -> None:
    planted = ast.parse(
        "def a(): ...\n@server.tool()\ndef b(): ...\n@_gated_tool('x', g)\nclass C: ...\n"
    )

    assert sorted(_front_door_violations(planted)) == [
        "definition C",
        "definition a",
        "definition b",
        "registration _gated_tool",
        "registration server.tool",
    ]


@pytest.mark.parametrize("path", sorted(_MI.glob("*.py")), ids=lambda p: p.name)
def test_no_mcp_interface_module_exceeds_the_line_limit(path: Path) -> None:
    line_count = len(path.read_text(encoding="utf-8").splitlines())

    assert line_count <= _MAX_MODULE_LINES, f"{path.name} has {line_count} lines"


def test_tool_group_modules_are_exactly_the_tools_modules_on_disk() -> None:
    front_door = importlib.import_module(f"{_PACKAGE}.mcp_server")

    registered = {module.__name__.rsplit(".", 1)[-1] for module in front_door.TOOL_GROUP_MODULES}

    assert registered == {path.stem for path in _MI.glob("*_tools.py")}
    assert len(front_door.TOOL_GROUP_MODULES) == len(registered)


def test_tools_list_presents_tools_in_the_pre_split_order() -> None:
    server = importlib.import_module(f"{_PACKAGE}.mcp_server").server

    assert [tool.name for tool in asyncio.run(server.list_tools())] == _EXPECTED_TOOL_ORDER
