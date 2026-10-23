"""Locate the source files of the MCP surface that hold tool code, by glob.

The surface is `tool_core.py`, one `*_tools.py` module per tool group and the `mcp_server.py` front
door. Source-reading tests scan the union of these files instead of naming one module, so they keep
seeing the code when it moves between modules, and a probe placed in any tool-group module is seen.
Every function takes an optional `directory` so a test can aim it at a temporary directory holding a
planted probe module (the guard of the guard).
"""

from __future__ import annotations

import ast
from pathlib import Path

import ps_service.mcp_interface as mcp_package

MI_DIR = Path(mcp_package.__file__).parent
_CORE_AND_FRONT_DOOR = frozenset({"tool_core.py", "mcp_server.py"})


def tool_source_paths(directory: Path = MI_DIR) -> list[Path]:
    """Sorted paths of the core, front-door and tool-group modules in `directory`."""
    return sorted(
        path
        for path in directory.glob("*.py")
        if path.name in _CORE_AND_FRONT_DOOR or path.name.endswith("_tools.py")
    )


def tool_source_trees(directory: Path = MI_DIR) -> list[ast.Module]:
    """The parsed module of each file `tool_source_paths` finds."""
    return [ast.parse(path.read_text(encoding="utf-8")) for path in tool_source_paths(directory)]


def merged_tool_tree(directory: Path = MI_DIR) -> ast.Module:
    """One `ast.Module` whose body is the concatenated bodies of `tool_source_trees`."""
    body = [node for tree in tool_source_trees(directory) for node in tree.body]
    return ast.Module(body=body, type_ignores=[])


def tool_source_text(directory: Path = MI_DIR) -> str:
    """The concatenated text of each file `tool_source_paths` finds."""
    return "\n".join(path.read_text(encoding="utf-8") for path in tool_source_paths(directory))
