"""Source guard: only the three sanctioned modules open a raw `psycopg` connection.

Every state-store caller goes through `persistence.connect_from_config`, which applies the
configured bounds. A raw `psycopg.connect(` anywhere else would be an unbounded wait, so the set of
modules containing one must equal the allow-list exactly: a new site fails, and so does a removed
one that leaves a stale entry.
"""

from __future__ import annotations

import ast
from pathlib import Path

import ps_service

_SERVICE_ROOT = Path(ps_service.__file__).parent
_ALLOWED_MODULES = frozenset(
    {
        "persistence/connection.py",
        "graph_gateway/provision.py",
        "passkey_signing/store.py",
    }
)


def _opens_a_raw_connection(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "connect"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "psycopg"
        ):
            return True
    return False


def test_raw_psycopg_connections_are_opened_only_by_the_sanctioned_modules() -> None:
    actual = {
        path.relative_to(_SERVICE_ROOT).as_posix()
        for path in _SERVICE_ROOT.rglob("*.py")
        if _opens_a_raw_connection(path)
    }

    assert actual == _ALLOWED_MODULES
