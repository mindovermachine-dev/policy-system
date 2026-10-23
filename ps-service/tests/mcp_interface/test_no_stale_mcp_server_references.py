"""Every `mcp_server` mention in `ps-service/src` names a symbol the front door still defines.

`mcp_server.py` is a thin front door that defines only `server`, `handle_mcp_tool_call` and
`TOOL_GROUP_MODULES`. After the split, a docstring, comment or import that says `mcp_server.<name>`
for any other name points at code that moved to `tool_core` or a `*_tools` module.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import ps_service
from ps_service.mcp_interface import mcp_server

_SRC = Path(ps_service.__file__).parent
_FRONT_DOOR = Path(mcp_server.__file__)
_DEFINED = frozenset({"server", "handle_mcp_tool_call", "TOOL_GROUP_MODULES"})
_MENTION = re.compile(r"mcp_server(?=(?P<rest>[^\n]*))")
_SYMBOL_AFTER = re.compile(r"^\.(?P<name>\w+)")
_IMPORT_AFTER = re.compile(r"^ import (?P<names>[\w, ]+)")


def _stale_mentions(text: str) -> list[str]:
    """Return each `mcp_server` mention in `text` that does not name a front-door symbol."""
    stale: list[str] = []
    for match in _MENTION.finditer(text):
        rest = match.group("rest")
        symbol = _SYMBOL_AFTER.match(rest)
        imported = _IMPORT_AFTER.match(rest)
        if symbol is not None and symbol.group("name") in _DEFINED:
            continue
        if (
            imported is not None
            and {n.strip() for n in imported.group("names").split(",")} <= _DEFINED
        ):
            continue
        stale.append(f"mcp_server{rest}")
    return stale


def test_the_front_door_defines_the_symbols_the_guard_allows() -> None:
    assert all(hasattr(mcp_server, name) for name in _DEFINED)


_SOURCES = sorted(path for path in _SRC.rglob("*.py") if path != _FRONT_DOOR)


@pytest.mark.parametrize("path", _SOURCES, ids=lambda p: str(p.relative_to(_SRC)))
def test_every_mcp_server_mention_names_a_symbol_the_front_door_defines(path: Path) -> None:
    assert _stale_mentions(path.read_text(encoding="utf-8")) == []


def test_the_stale_mention_checker_flags_planted_mentions_and_only_those() -> None:
    planted = (
        "from ps_service.mcp_interface.mcp_server import server\n"
        "wraps mcp_server.server and mcp_server.handle_mcp_tool_call\n"
        "patch mcp_server._run_mcp_action here\n"
        "the `mcp_server` singleton\n"
        "from ps_service.mcp_interface.mcp_server import _gate_environment\n"
    )

    assert _stale_mentions(planted) == [
        "mcp_server._run_mcp_action here",
        "mcp_server` singleton",
        "mcp_server import _gate_environment",
    ]
