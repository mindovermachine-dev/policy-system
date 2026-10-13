"""Only the gateway package may build a write-capable FalkorDB client (issue #215, AC-BI-001).

Every module under `ps-service/src/ps_service` outside `graph_gateway/` is scanned with the shared
`write_path_scan.scan_source`: no `falkordb` / `redis` import (beyond `redis.exceptions`), no
`FalkorDB(...)` construction, no import of the gateway's write-capable factory
(`graph_gateway.connection`, `graph_gateway.key_port`) and no write Cypher passed to a graph
handle. One test per module, so a failure names the module (`<dotted module>: <finding>`).

There is no allow-list and no per-package exemption: the connection helpers of the components are
read-only delegates and the per-package guards (`test_no_graph_write_path.py`) use this same scan.
Excluded by construction (they lie outside the scanned tree): test code and the disposable
container fixtures under `ps-service/tests`, `tools/` (the curated-export CLI keeps its own
FalkorDB use by specification) and `spikes/`. The only package excluded inside the tree is
`graph_gateway/` itself.

The guard walks the tree with the same `iter_service_modules` helper as the Cypher vocabulary scan
(`domain_schema/test_cypher_vocabulary_scan.py`, #198, AC-BI-016), so the two cannot diverge.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

import ps_service
from graph_gateway.write_path_scan import iter_service_modules, scan_source

_SRC_ROOT = Path(ps_service.__file__).parent
_TESTS_ROOT = Path(__file__).resolve().parents[1]
_GATEWAY_PACKAGE = "graph_gateway"
_PER_PACKAGE_HELPERS = (
    "_CONNECTION_HELPER",
    "_TYPE_ONLY_IMPORTS",
    "_READ_SIDE_IMPORTS",
    "_FORBIDDEN_IMPORT_PREFIXES",
    "_CONNECTION_CALLS",
    "scan_source",
    "_call_findings",
)


def _dotted(path: Path, root: Path) -> str:
    return ".".join(("ps_service", *path.relative_to(root).with_suffix("").parts))


def _modules_outside_the_gateway(root: Path) -> dict[str, Path]:
    return {
        _dotted(path, root): path
        for path in iter_service_modules(root)
        if _GATEWAY_PACKAGE not in path.relative_to(root).parts
    }


def _report(root: Path) -> list[str]:
    """One `<dotted module>: <finding>` line per finding outside the gateway package."""
    return [
        f"{module}: {finding}"
        for module, path in _modules_outside_the_gateway(root).items()
        for finding in scan_source(path.read_text(encoding="utf-8"))
    ]


@pytest.mark.parametrize("module", sorted(_modules_outside_the_gateway(_SRC_ROOT)))
def test_no_module_outside_the_gateway_reaches_falkordb_directly(module: str) -> None:
    path = _modules_outside_the_gateway(_SRC_ROOT)[module]

    assert [f"{module}: {finding}" for finding in scan_source(path.read_text("utf-8"))] == []


def test_the_whole_tree_reports_nothing() -> None:
    assert _report(_SRC_ROOT) == []


def test_the_gateway_package_is_the_only_tree_excluded() -> None:
    scanned = set(_modules_outside_the_gateway(_SRC_ROOT))
    every = {_dotted(path, _SRC_ROOT) for path in iter_service_modules(_SRC_ROOT)}

    assert "ps_service.main" in scanned
    assert every - scanned
    assert all(module.startswith("ps_service.graph_gateway.") for module in every - scanned)


def test_a_module_planted_in_a_copied_tree_is_named_in_the_report(tmp_path: Path) -> None:
    copy = tmp_path / "ps_service"
    shutil.copytree(_SRC_ROOT, copy, ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "restore" / "planted_driver.py").write_text("import falkordb\n", encoding="utf-8")

    report = _report(copy)

    assert len(report) == 1
    assert report[0].startswith("ps_service.restore.planted_driver: ")


def test_a_module_planted_inside_the_gateway_package_is_not_reported(tmp_path: Path) -> None:
    copy = tmp_path / "ps_service"
    shutil.copytree(_SRC_ROOT, copy, ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "graph_gateway" / "planted_driver.py").write_text("import falkordb\n", "utf-8")

    assert _report(copy) == []


@pytest.mark.parametrize(
    ("planted", "expected"),
    [
        ("import falkordb\n", "imports falkordb"),
        ("from redis import Redis\n", "imports redis"),
        ("import redis\n", "imports redis"),
        ("db = FalkorDB(host='h', port=1)\n", "calls FalkorDB"),
        ("from ps_service.graph_gateway.connection import connect\n", "imports"),
        ("from ps_service.graph_gateway import key_port\n", "imports"),
        ('def f(g):\n    g.query("MERGE (n:X) SET n.a = 1")\n', "write Cypher passed to query"),
    ],
    ids=[
        "falkordb_import",
        "redis_from_import",
        "redis_import",
        "falkordb_construction",
        "write_capable_factory",
        "key_port",
        "write_cypher",
    ],
)
def test_a_planted_violation_is_reported(planted: str, expected: str) -> None:
    assert any(expected in finding for finding in scan_source(planted))


def test_a_falkordb_import_under_type_checking_is_flagged() -> None:
    planted = "if TYPE_CHECKING:\n    from falkordb import Graph\n"

    assert any("imports falkordb" in finding for finding in scan_source(planted))


def test_redis_exceptions_and_prose_are_not_flagged() -> None:
    clean = (
        '"""Mentions import falkordb and MERGE only in prose."""\n'
        "from redis.exceptions import ResponseError\n"
        'MESSAGE = "cannot MERGE here"\n'
    )

    assert scan_source(clean) == []


def test_the_guard_and_the_vocabulary_scan_walk_the_same_modules() -> None:
    scan = _TESTS_ROOT / "domain_schema" / "test_cypher_vocabulary_scan.py"
    vocabulary = scan.read_text(encoding="utf-8")

    assert "iter_service_modules" in vocabulary
    assert "rglob" not in vocabulary


def test_no_per_package_exemption_remains() -> None:
    offenders: list[str] = []
    for guard in sorted(_TESTS_ROOT.glob("*/test_no_graph_write_path.py")):
        tree = ast.parse(guard.read_text(encoding="utf-8"))
        defined = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        } | {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        keywords = {
            keyword.arg
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
        }
        offenders += [
            f"{guard.parent.name}: defines {name}"
            for name in _PER_PACKAGE_HELPERS
            if name in defined
        ]
        offenders += [
            f"{guard.parent.name}: passes {name}"
            for name in ("allow_connection", "allow_imports", "is_connection_helper")
            if name in keywords
        ]

    assert offenders == []
