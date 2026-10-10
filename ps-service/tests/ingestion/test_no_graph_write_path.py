"""Ingestion holds no write path to FalkorDB (issue #208, AC-BI-001, F10).

Every graph mutation goes through the Graph Write Gateway, so no module under
`ps_service.ingestion` (adapters included) may open a FalkorDB connection of its own, name the
gateway's apply machinery, or pass write Cypher to a graph handle. The scan is an AST walk: it
looks at the string literals (and the constant parts of f-strings) handed to a call named
`query`, `ro_query`, `_execute_query` or `_read_query`, never at docstrings or prose, so a module
may *describe* `MERGE` without being flagged. The one place allowed to touch the FalkorDB driver
is `falkordb_client.py`, the connection helper whose handles ingestion only reads through.

The guard is itself guarded: planted violations must be found, and prose must not be.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import ps_service.ingestion as ingestion_package

_INGESTION_ROOT = Path(ingestion_package.__file__).parent
_CONNECTION_HELPER = "falkordb_client.py"
_QUERY_CALLS = frozenset({"query", "ro_query", "_execute_query", "_read_query"})
_CONNECTION_CALLS = frozenset({"FalkorDB", "connect", "connect_from_config", "select_graph"})
_WRITE_CYPHER = re.compile(r"\b(MERGE|CREATE|SET|DELETE|REMOVE|DROP|DETACH)\b")
_FORBIDDEN_IMPORT_PREFIXES = (
    "falkordb",
    "ps_service.graph_gateway.applier",
    "ps_service.graph_gateway.cypher",
)
_FORBIDDEN_NAMES = frozenset({"apply_entries"})


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _literal_text(node: ast.expr) -> str | None:
    """The constant text of a string literal or f-string (placeholders dropped), else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return " ".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def _imported_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.module is None or node.level != 0:
        return []
    return [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]


def _call_findings(node: ast.Call, *, is_connection_helper: bool) -> list[str]:
    name = _called_name(node)
    findings: list[str] = []
    if name in _FORBIDDEN_NAMES or (name in _CONNECTION_CALLS and not is_connection_helper):
        findings.append(f"line {node.lineno}: calls {name}")
    if name in _QUERY_CALLS:
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        findings += [
            f"line {node.lineno}: write Cypher passed to {name}: {text!r}"
            for argument in arguments
            if (text := _literal_text(argument)) is not None and _WRITE_CYPHER.search(text)
        ]
    return findings


def scan_source(source: str, *, is_connection_helper: bool = False) -> list[str]:
    """Return one finding per write path in `source` (empty: the module is clean)."""
    findings: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import | ast.ImportFrom) and not is_connection_helper:
            findings += [
                f"line {node.lineno}: imports {module}"
                for module in _imported_modules(node)
                if module.startswith(_FORBIDDEN_IMPORT_PREFIXES)
            ]
        elif isinstance(node, ast.Call):
            findings += _call_findings(node, is_connection_helper=is_connection_helper)
    return findings


def _ingestion_modules() -> list[Path]:
    return sorted(path for path in _INGESTION_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def test_the_scan_covers_the_ingestion_package_and_its_adapters() -> None:
    names = {path.relative_to(_INGESTION_ROOT).as_posix() for path in _ingestion_modules()}

    assert {"pipeline.py", "graph_writer.py", "falkordb_client.py"} <= names
    assert "adapters/internal_seed/persist.py" in names


@pytest.mark.parametrize(
    "path", _ingestion_modules(), ids=lambda path: path.relative_to(_INGESTION_ROOT).as_posix()
)
def test_no_ingestion_module_opens_a_write_connection_or_passes_write_cypher(path: Path) -> None:
    findings = scan_source(
        path.read_text(encoding="utf-8"), is_connection_helper=path.name == _CONNECTION_HELPER
    )

    assert findings == []


# --- the guard of the guard: it must fail on a planted violation, and only on one ---


@pytest.mark.parametrize(
    "source",
    [
        'def f(g):\n    g.query("MERGE (n:Policy {id: $id})", {"id": 1})\n',
        'def f(g, label):\n    g.ro_query(f"CREATE (n:{label})")\n',
        'def f(g):\n    g._execute_query(query="MATCH (n) DETACH DELETE n")\n',
        'def f(g):\n    _read_query(g, "MATCH (n) SET n.x = 1")\n',
        "from falkordb import FalkorDB\n",
        "from ps_service.graph_gateway.applier import apply_entries\n",
        "def f(db):\n    return db.select_graph('x')\n",
        "def f(config):\n    return connect_from_config(config)\n",
    ],
    ids=[
        "merge",
        "create_fstring",
        "delete_keyword",
        "set_via_read_helper",
        "driver_import",
        "applier_import",
        "select_graph",
        "connect",
    ],
)
def test_the_guard_finds_a_planted_violation(source: str) -> None:
    assert len(scan_source(source)) >= 1


def test_the_guard_finds_exactly_one_finding_for_one_planted_write_query() -> None:
    assert len(scan_source('def f(g):\n    g.query("MERGE (n)")\n')) == 1


@pytest.mark.parametrize(
    "source",
    [
        'def f():\n    """MERGE and CREATE live in the gateway, not here: SET DELETE."""\n',
        '# MERGE (n)\nX = "MERGE (n)"\n',
        'def f(g):\n    g.query("MATCH (n:Policy) WHERE n.id IN $ids RETURN n.id", {"ids": []})\n',
        "def f(g, q):\n    g.query(q)\n",
        'def f():\n    raise ValueError("cannot SET the property")\n',
    ],
    ids=["docstring", "comment_and_assignment", "read_query", "variable_query", "message_only"],
)
def test_the_guard_ignores_prose_and_read_queries(source: str) -> None:
    assert scan_source(source) == []


def test_the_connection_helper_may_touch_the_driver_but_not_pass_write_cypher() -> None:
    helper = "from falkordb import FalkorDB\ndef connect(host):\n    return FalkorDB(host=host)\n"

    assert scan_source(helper, is_connection_helper=True) == []
    assert (
        len(scan_source('def f(g):\n    g.query("CREATE (n)")\n', is_connection_helper=True)) == 1
    )
