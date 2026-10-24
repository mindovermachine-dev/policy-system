"""Source guard: every FalkorDB connector call in the service names both timeouts.

The factory requires `socket_connect_timeout` and `socket_timeout`; a call that spreads a mapping
into the keywords is accepted. A new caller that omits them would reintroduce an unbounded wait,
so the scan fails for it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import ps_service

_SERVICE_ROOT = Path(ps_service.__file__).parent
_CONNECTORS = frozenset({"connect", "open_read_only_database"})
_REQUIRED_KEYWORDS = frozenset({"socket_connect_timeout", "socket_timeout"})


def _connector_calls_missing_timeouts(path: Path) -> list[int]:
    missing: list[int] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in _CONNECTORS:
            continue
        keywords = {keyword.arg for keyword in node.keywords}
        spreads_a_mapping = None in keywords
        if not spreads_a_mapping and not keywords >= _REQUIRED_KEYWORDS:
            missing.append(node.lineno)
    return missing


def test_every_connector_call_in_the_service_passes_both_timeouts() -> None:
    offenders = sorted(
        f"{path.relative_to(_SERVICE_ROOT)}:{line}"
        for path in _SERVICE_ROOT.rglob("*.py")
        for line in _connector_calls_missing_timeouts(path)
    )

    assert offenders == []
