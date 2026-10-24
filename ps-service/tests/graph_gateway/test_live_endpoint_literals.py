"""Source guard: `falkordb_live` tests reach FalkorDB only through `falkordb_endpoint()`.

`falkordb_endpoint()` honours `PS_TEST_FALKORDB_HOST` and `PS_TEST_FALKORDB_PORT`, so the live
suite can run against a throwaway container on any port. A live test that opens a connection
with a literal port, or with a module constant bound to one, always dials that port and cannot be
redirected.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[1]
_CONNECTORS = frozenset({"connect", "connect_writable", "FalkorDB"})
_LIVE_MARKER = "falkordb_live"


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _literal_int_constants(tree: ast.Module) -> frozenset[str]:
    names: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Constant):
            if isinstance(statement.value.value, int):
                names.update(t.id for t in statement.targets if isinstance(t, ast.Name))
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, int)
        ):
            names.add(statement.target.id)
    return frozenset(names)


def _port_argument(call: ast.Call) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == "port":
            return keyword.value
    return call.args[1] if len(call.args) > 1 else None


def _fixed_port_connections(source_root: Path) -> list[str]:
    """Return `<relative path>:<line>` for each connection with a fixed port in a live file."""
    findings: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if _LIVE_MARKER not in text:
            continue
        tree = ast.parse(text)
        constants = _literal_int_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _called_name(node) not in _CONNECTORS:
                continue
            port = _port_argument(node)
            fixed = (isinstance(port, ast.Constant) and isinstance(port.value, int)) or (
                isinstance(port, ast.Name) and port.id in constants
            )
            if fixed:
                findings.append(f"{path.relative_to(source_root)}:{node.lineno}")
    return findings


def test_no_live_test_connects_to_falkordb_on_a_fixed_port() -> None:
    """Every live FalkorDB connection takes its port from the live-test endpoint."""
    assert _fixed_port_connections(_TESTS) == []


def test_the_scan_flags_literal_and_constant_ports_in_live_files_only(tmp_path: Path) -> None:
    """Literal and constant ports in live files are flagged; resolved ports, fast files are not."""
    marker = f"pytestmark = pytest.mark.{_LIVE_MARKER}\n"
    (tmp_path / "literal.py").write_text(
        f"{marker}db = connect(host='127.0.0.1', port=6379)\n", encoding="utf-8"
    )
    (tmp_path / "constant.py").write_text(
        f"{marker}_PORT = 6379\ndb = client.connect_writable(host='h', port=_PORT)\n",
        encoding="utf-8",
    )
    (tmp_path / "positional.py").write_text(
        f"{marker}db = FalkorDB('127.0.0.1', 6379)\n", encoding="utf-8"
    )
    (tmp_path / "resolved.py").write_text(
        f"{marker}host, port = falkordb_endpoint()\ndb = connect(host=host, port=port)\n"
        "check_connectivity(probe, host='127.0.0.1', port=6379)\n",
        encoding="utf-8",
    )
    (tmp_path / "fast.py").write_text("db = connect(host='127.0.0.1', port=6379)\n", "utf-8")

    assert _fixed_port_connections(tmp_path) == [
        "constant.py:3",
        "literal.py:2",
        "positional.py:2",
    ]
