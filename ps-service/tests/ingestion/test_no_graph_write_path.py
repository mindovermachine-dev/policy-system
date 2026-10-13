"""Ingestion holds no write path to FalkorDB (issue #208, AC-BI-001, F10).

Every graph mutation goes through the Graph Write Gateway, so no module under
`ps_service.ingestion` (adapters included) may open a FalkorDB connection of its own, name the
gateway's apply machinery, or pass write Cypher to a graph handle. The scan is an AST walk: it
looks at the string literals (and the constant parts of f-strings) handed to a call named
`query`, `ro_query`, `_execute_query` or `_read_query`, never at docstrings or prose, so a module
may *describe* `MERGE` without being flagged. The one place allowed to touch the FalkorDB driver
is the gateway package; `falkordb_client.py` is a read-only delegate scanned like the others.

The guard is itself guarded: planted violations must be found, and prose must not be.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from graph_gateway.write_path_scan import scan_source

import ps_service.ingestion as ingestion_package

_INGESTION_ROOT = Path(ingestion_package.__file__).parent


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
    assert scan_source(path.read_text(encoding="utf-8")) == []


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
        "def f():\n    return FalkorDB(host='h', port=1)\n",
        "from ps_service.graph_gateway.connection import connect\n",
    ],
    ids=[
        "merge",
        "create_fstring",
        "delete_keyword",
        "set_via_read_helper",
        "driver_import",
        "applier_import",
        "construction",
        "write_capable_factory",
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


def test_the_former_connection_helper_gets_no_allowance() -> None:
    helper = "from falkordb import FalkorDB\ndef connect(host):\n    return FalkorDB(host=host)\n"

    assert len(scan_source(helper)) >= 2
