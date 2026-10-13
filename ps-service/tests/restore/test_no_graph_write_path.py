"""Restore holds no write path to FalkorDB (issue #214, AC-BI-001, AC-BI-008, CHANGES.md A-2).

A restore changes the graphs only through the Graph Write Gateway: one multi-graph group, built
as primitives (`leg_primitives`, `merge_primitives`) and submitted by `graph_submission`. So no
module of `ps_service.restore` may open a FalkorDB connection, name the gateway's apply
machinery, or pass write Cypher to a graph handle. The scan is `graph_gateway.write_path_scan`
(an AST walk of the literals passed to `query` and its siblings, never docstrings or prose).

The three pre-gateway modules (`populate.py`, `staging.py`, `snapshot_merge_writer.py`) were
deleted by issue #215 slice S6 after the live comparison; `test_no_legacy_restore_module_remains`
keeps them gone and `_used_legacy` keeps anything from importing or calling their entry points.

Positive half: `restore_instrument` reaches `submit_restore`, which reaches the gateway's
`submit_multi_graph_group`.

The guard is itself guarded: planted violations must be found, and prose must not be.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from graph_gateway.write_path_scan import reaches, scan_source

import ps_service.api as api_package
import ps_service.mcp_interface as mcp_package
import ps_service.restore as restore_package

_ROOT = Path(restore_package.__file__).parent
_API = Path(api_package.__file__).parent
_MCP = Path(mcp_package.__file__).parent
_LEGACY_FILES = frozenset({"populate.py", "staging.py", "snapshot_merge_writer.py"})
_LEGACY_MODULES = frozenset(
    f"ps_service.restore.{name.removesuffix('.py')}" for name in _LEGACY_FILES
)
_LEGACY_NAMES = frozenset(
    {
        "populate_graph",
        "stage_graph",
        "snapshot_single_tenant",
        "finalize_atomic_swap",
        "discard_staged_keys",
        "stage_and_finalize_policy_system_leg",
        "_run_baseline_merge",
    }
)
_SINK = "submit_multi_graph_group"


def _modules() -> list[Path]:
    return sorted(path for path in _ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def _checked() -> list[Path]:
    return _modules()


def _param(path: Path) -> object:
    return pytest.param(path, id=f"{path.parent.name}/{path.name}")


def _used_legacy(tree: ast.AST) -> list[str]:
    """Imports of the legacy modules and uses of their entry points (never docstrings)."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits.extend(a.name for a in node.names if a.name in _LEGACY_MODULES)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in _LEGACY_MODULES:
                hits.append(module)
            elif module == "ps_service.restore":
                hits.extend(
                    f"{module}.{a.name}"
                    for a in node.names
                    if f"{module}.{a.name}" in _LEGACY_MODULES
                )
            hits.extend(a.name for a in node.names if a.name in _LEGACY_NAMES)
        elif isinstance(node, ast.Name) and node.id in _LEGACY_NAMES:
            hits.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in _LEGACY_NAMES:
            hits.append(node.attr)
    return hits


def test_the_scan_covers_the_restore_package() -> None:
    names = {path.relative_to(_ROOT).as_posix() for path in _checked()}

    assert {
        "restore_instrument.py",
        "graph_submission.py",
        "leg_primitives.py",
        "merge_primitives.py",
        "baseline_reader.py",
        "schema_allowlist.py",
        "errors.py",
    } <= names
    assert not names & _LEGACY_FILES


@pytest.mark.parametrize("path", [_param(path) for path in _checked()])
def test_no_restore_module_opens_a_connection_or_passes_write_cypher(path: Path) -> None:
    findings = scan_source(path.read_text(encoding="utf-8"))

    assert findings == []


def test_no_legacy_restore_module_remains() -> None:
    assert {path.name for path in _modules()} & _LEGACY_FILES == set()


@pytest.mark.parametrize(
    "path",
    [
        _param(path)
        for path in (
            *_checked(),
            *_API.glob("restore*.py"),
            _API / "routes_restorations.py",
            _MCP / "mcp_server.py",
        )
        if path.is_file()
    ],
)
def test_nothing_in_the_restore_path_uses_the_deleted_modules(path: Path) -> None:
    assert _used_legacy(ast.parse(path.read_text(encoding="utf-8"))) == []


def test_the_rest_orchestration_passes_no_write_cypher() -> None:
    source = (_API / "restore_orchestration.py").read_text(encoding="utf-8")

    # Its function-local `connect_from_config` opens the READ handle; only writes are forbidden.
    findings = [f for f in scan_source(source) if "write Cypher" in f or "apply" in f]
    assert findings == []


# --- the positive half: the group reaches the gateway ---


def test_restore_instrument_reaches_the_submit_and_the_submit_reaches_the_gateway() -> None:
    entry = (_ROOT / "restore_instrument.py").read_text(encoding="utf-8")
    submission = (_ROOT / "graph_submission.py").read_text(encoding="utf-8")

    assert reaches(entry, "restore_instrument", "submit_restore")
    assert reaches(submission, "submit_restore", _SINK)


# --- the guard of the guard: it must fail on a planted violation, and only on one ---


@pytest.mark.parametrize(
    "source",
    [
        'def f(g):\n    g.query("UNWIND $rows AS row CREATE (n:X) SET n = row", {})\n',
        'def f(g, label):\n    g.ro_query(f"MERGE (n:{label})")\n',
        "from falkordb import FalkorDB\n",
        "from ps_service.graph_gateway.applier import apply_entries\n",
        "from ps_service.graph_gateway.connection import connect\n",
    ],
    ids=["unwind_create", "merge_fstring", "driver_import", "applier_import", "factory"],
)
def test_the_scan_finds_a_planted_violation(source: str) -> None:
    assert len(scan_source(source)) >= 1


@pytest.mark.parametrize(
    "source",
    [
        "from ps_service.restore.staging import finalize_atomic_swap\n",
        "from ps_service.restore import populate\n",
        "import ps_service.restore.snapshot_merge_writer\n",
        "def f(g):\n    return populate_graph(g, None)\n",
        "def f(m):\n    return m._run_baseline_merge()\n",
    ],
    ids=["from_staging", "package_import", "plain_import", "call", "attribute"],
)
def test_the_legacy_check_finds_a_planted_use(source: str) -> None:
    assert _used_legacy(ast.parse(source)) != []


@pytest.mark.parametrize(
    "source",
    [
        '"""Writes MERGE and SET in prose; populate_graph is described, not run."""\nx = 1\n',
        'def f(g):\n    g.query("MATCH (n:Capability) RETURN n.id")\n',
    ],
    ids=["prose", "read"],
)
def test_the_guards_ignore_prose_and_reads(source: str) -> None:
    assert scan_source(source) == []
    assert _used_legacy(ast.parse(source)) == []
