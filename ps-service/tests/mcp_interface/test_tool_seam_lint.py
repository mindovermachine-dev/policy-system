"""No test patches a store class or the connection opener on the MCP tool seam.

A whole-tree lint (a deliberate exception to the one-to-one `tests/` to `src/` mirror, like
`test_docs_name_the_split_modules.py`). The tool modules receive their stores and graph opener
through one `GateEnvironment`; a test that overrides `PsycopgXStore` or `connect_from_config` on
`tool_core`, `mcp_server` or a `*_tools` module is patching a name the tools no longer read. Patches
of the same names on other components (`graph_cleanup.dependencies`, `api.dependencies`) are not the
tool seam and stay allowed.

The check is syntactic: it resolves import aliases, reads attribute and dotted-string targets, and
sees multi-line calls, `setattr`, `mock.patch` and `patch.object`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import ps_service.mcp_interface as mcp_package
from ps_service.mcp_interface import tool_core

_REPO = Path(__file__).resolve().parents[3]
_MI = Path(mcp_package.__file__).parent
_TEST_ROOTS = (_REPO / "ps-service" / "tests", _REPO / "ps-cli" / "tests")
_SEAM_MODULES = frozenset({"tool_core", "mcp_server"})
_SEAM_NAME = re.compile(r"^(Psycopg\w+Store|connect_from_config)$")
_PACKAGE_PREFIX = "ps_service.mcp_interface."


def _is_seam_module(basename: str) -> bool:
    return basename in _SEAM_MODULES or basename.endswith("_tools")


def _alias_map(tree: ast.AST) -> dict[str, str]:
    """Map each name an import binds to the basename of the module it refers to."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                aliases[bound] = alias.name.split(".")[-1] if alias.asname else bound
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
    return aliases


def _module_basename(node: ast.expr, aliases: dict[str, str]) -> str:
    """The module basename an expression names: an aliased name or the tail of a dotted path."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_patching_call(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id in {"setattr", "patch"}
    if isinstance(func, ast.Attribute):
        if func.attr in {"setattr", "patch"}:
            return True
        return (
            func.attr == "object"
            and isinstance(func.value, (ast.Name, ast.Attribute))
            and _module_basename(func.value, {}) == "patch"
        )
    return False


def _string(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _target_problem(args: list[ast.expr], aliases: dict[str, str]) -> str | None:
    """The seam target a call's positional arguments name, or None."""
    dotted = _string(args[0]) if args else None
    if dotted is not None and dotted.startswith(_PACKAGE_PREFIX):
        module, _, name = dotted.removeprefix(_PACKAGE_PREFIX).partition(".")
        if _is_seam_module(module) and _SEAM_NAME.match(name):
            return f"{module}.{name}"
        return None
    if len(args) >= 2:  # (owner, "Name") pair
        name = _string(args[1])
        module = _module_basename(args[0], aliases)
        if name is not None and _is_seam_module(module) and _SEAM_NAME.match(name):
            return f"{module}.{name}"
    return None


def seam_patches(source: str) -> list[str]:
    """Return `module.Name` for each patch of a seam name on the tool seam in `source`."""
    tree = ast.parse(source)
    aliases = _alias_map(tree)
    problems: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_patching_call(node.func)):
            continue
        args = list(node.args)
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        if "target" in keywords:
            args = [keywords["target"], *args]
        if "name" in keywords and len(args) == 1:
            args = [*args, keywords["name"]]
        problem = _target_problem(args, aliases)
        if problem is not None:
            problems.append(problem)
    return problems


def _test_files() -> list[Path]:
    return sorted(path for root in _TEST_ROOTS for path in root.rglob("*.py"))


_FILES = _test_files()


@pytest.mark.parametrize("path", _FILES, ids=[str(p.relative_to(_REPO)) for p in _FILES])
def test_no_test_patches_a_store_class_or_the_connection_opener_on_the_tool_seam(
    path: Path,
) -> None:
    assert seam_patches(path.read_text(encoding="utf-8")) == []


def _src(*lines: str) -> str:
    return "\n".join(lines) + "\n"


_DOTTED_TOOL_CORE = "ps_service.mcp_interface.tool_core"
_DOTTED_CATALOG = "ps_service.mcp_interface.catalog_tools"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            _src("def t(mp):", "    mp.setattr(tool_core, 'PsycopgAuditStore', f)"),
            ["tool_core.PsycopgAuditStore"],
        ),
        (
            _src(
                "from ps_service.mcp_interface import tool_core as core",
                "def t(mp):",
                "    mp.setattr(core, 'connect_from_config', f)",
            ),
            ["tool_core.connect_from_config"],
        ),
        (
            _src("def t(mp):", f"    mp.setattr('{_DOTTED_CATALOG}.PsycopgAccessRoleStore', f)"),
            ["catalog_tools.PsycopgAccessRoleStore"],
        ),
        (
            _src(
                "def t(mp):",
                "    mp.setattr(",
                "        mcp_server,",
                "        'PsycopgPendingApprovalStore',",
                "        f,",
                "    )",
            ),
            ["mcp_server.PsycopgPendingApprovalStore"],
        ),
        (
            _src(
                "def t():",
                "    with patch.object(cypher_tools, 'PsycopgAuditStore'):",
                "        pass",
            ),
            ["cypher_tools.PsycopgAuditStore"],
        ),
        (
            _src(
                "def t():",
                f"    with mock.patch('{_DOTTED_TOOL_CORE}.connect_from_config'):",
                "        pass",
            ),
            ["tool_core.connect_from_config"],
        ),
        (
            _src(
                "def t(mp):",
                "    mp.setattr(ps_service.mcp_interface.tool_core, 'PsycopgAuditStore', f)",
            ),
            ["tool_core.PsycopgAuditStore"],
        ),
        (
            _src(
                "def t(mp):",
                f"    mp.setattr(target='{_DOTTED_TOOL_CORE}.PsycopgAuditStore', value=f)",
            ),
            ["tool_core.PsycopgAuditStore"],
        ),
    ],
    ids=[
        "attribute",
        "aliased-module",
        "dotted-string",
        "multi-line",
        "patch-object",
        "mock-patch-string",
        "dotted-attribute-owner",
        "target-keyword",
    ],
)
def test_the_lint_flags_a_planted_seam_patch(source: str, expected: list[str]) -> None:
    assert seam_patches(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        _src("def t(mp):", "    mp.setattr(graph_cleanup.dependencies, 'PsycopgAuditStore', f)"),
        _src(
            "def t(mp):",
            "    mp.setattr('ps_service.graph_cleanup.dependencies.PsycopgAuditStore', f)",
        ),
        _src("def t(mp):", "    mp.setattr(tool_core, 'load_config', f)"),
        _src("def t(mp):", "    mp.setattr(api_dependencies, 'PsycopgAccessRoleStore', f)"),
    ],
    ids=["other-component", "other-component-string", "non-seam-name", "api-dependencies"],
)
def test_the_lint_allows_patches_of_other_components_and_non_seam_names(source: str) -> None:
    assert seam_patches(source) == []


def test_exactly_one_environment_class_is_defined_in_the_mcp_interface_package() -> None:
    classes = [
        (path.name, node.name)
        for path in sorted(_MI.glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef) and node.name.endswith("Environment")
    ]

    assert classes == [("tool_gates.py", "GateEnvironment")]


def test_tool_core_binds_no_store_class_and_no_connection_opener() -> None:
    bound = [name for name in dir(tool_core) if _SEAM_NAME.match(name)]
    exported = [name for name in tool_core.__all__ if _SEAM_NAME.match(name)]

    assert (bound, exported) == ([], [])
