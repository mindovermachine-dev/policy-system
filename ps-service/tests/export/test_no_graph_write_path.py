"""Export holds no write path to FalkorDB (issue #213, AC-BI-001, AC-BI-007).

Export's only graph write, the embedding backfill, goes through the Graph Write Gateway, so no
module under `ps_service.export` may open a FalkorDB connection of its own, name the gateway's
apply machinery, or pass write Cypher to a graph handle. The scan is
`graph_gateway.write_path_scan` (an AST walk of the literals passed to `query` and its siblings,
never docstrings or prose). No module is
exempt: `falkordb_connection.py` is a read-only handle over the gateway and is scanned like the
others. The REST orchestration (`api/export_orchestration.py`) is scanned too, and the maintainer
CLI shim (`tools/curated-export/export_instrument.py`) is checked for write Cypher only: its own
read connection stays outside the architecture guard by specification.

Positive half: the backfill reaches the gateway sink `submit_or_raise`, `export_instrument`
reaches the backfill, and `run_export` hands the gateway and the audit row to the delegate.

Symbol absence (AC-BI-001): the pre-#213 direct writer `_write_embedding` is named neither in the
source nor in the tests.

The guard is itself guarded: planted violations must be found, and prose must not be.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from graph_gateway.write_path_scan import reaches, scan_source

import ps_service.api as api_package
import ps_service.export as export_package

_ROOT = Path(export_package.__file__).parent
_API = Path(api_package.__file__).parent
_TESTS = Path(__file__).parent
_CLI = Path(__file__).resolve().parents[3] / "tools" / "curated-export" / "export_instrument.py"
_GATEWAY_SINK = "submit_or_raise"
_REMOVED_SYMBOLS = re.compile(r"\b_write_embedding\b")
_THIS_FILE = Path(__file__).name


def _modules() -> list[Path]:
    return sorted(path for path in _ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def _module_param(path: Path) -> object:
    return pytest.param(path, id=path.relative_to(_ROOT).as_posix())


def test_the_scan_covers_the_export_package() -> None:
    names = {path.relative_to(_ROOT).as_posix() for path in _modules()}

    assert {
        "embeddings.py",
        "export_instrument.py",
        "serialize.py",
        "catalog_writer.py",
        "falkordb_connection.py",
    } <= names


@pytest.mark.parametrize("path", [_module_param(path) for path in _modules()])
def test_no_export_module_opens_a_write_connection_or_passes_write_cypher(path: Path) -> None:
    assert scan_source(path.read_text(encoding="utf-8")) == []


def test_the_rest_orchestration_is_clean_under_the_shared_scan() -> None:
    assert scan_source((_API / "export_orchestration.py").read_text(encoding="utf-8")) == []


def test_the_cli_shim_passes_no_write_cypher() -> None:
    # The shim is a script-level composition root with its own read connection, outside the
    # architecture guard by specification (it is not part of the service tree). It must still
    # issue no write Cypher and name no apply machinery.
    findings = scan_source(_CLI.read_text(encoding="utf-8"))

    assert [f for f in findings if "imports falkordb" not in f and "calls FalkorDB" not in f] == []


def test_the_backfill_reaches_the_gateway_sink() -> None:
    source = (_ROOT / "embeddings.py").read_text(encoding="utf-8")

    assert reaches(source, "backfill_capability_embeddings", _GATEWAY_SINK)


def test_export_instrument_reaches_the_backfill() -> None:
    source = (_ROOT / "export_instrument.py").read_text(encoding="utf-8")

    assert reaches(source, "export_instrument", "backfill_capability_embeddings")


def test_run_export_gives_the_delegate_the_gateway_and_the_audit_anchor() -> None:
    source = (_API / "export_orchestration.py").read_text(encoding="utf-8")

    assert reaches(source, "run_export", "system_audit")
    for keyword in ("submitter=", "audit_event_id=", "baseline_graph_name="):
        assert keyword in source


# --- AC-BI-001: the direct writer's symbol is gone ---


def test_no_removed_symbol_is_named_in_the_source_or_the_tests() -> None:
    files = [
        *_modules(),
        *(p for p in _TESTS.rglob("*.py") if p.name != _THIS_FILE),
        *_API.glob("export*.py"),
        _CLI,
    ]

    hits = [
        f"{path.name}: {match.group(0)}"
        for path in files
        for match in _REMOVED_SYMBOLS.finditer(path.read_text(encoding="utf-8"))
    ]

    assert hits == []


def test_the_symbol_scan_finds_a_planted_reference() -> None:
    assert _REMOVED_SYMBOLS.search("_write_embedding(graph, label, node_id, vector)")


# --- the guard of the guard: it must fail on a planted violation, and only on one ---


@pytest.mark.parametrize(
    "source",
    [
        'def f(g):\n    g.query("MATCH (n:Capability {id: $id}) SET n.embedding = $e", {})\n',
        'def f(g, label):\n    g.ro_query(f"CREATE (n:{label})")\n',
        'def f(g):\n    g.query(query="MATCH (n) DETACH DELETE n")\n',
        'Q = "MATCH (n) SET n.embedding = $e"\ndef f(g):\n    g.query(Q)\n',
        "from falkordb import FalkorDB\n",
        "from ps_service.graph_gateway.applier import apply_entries\n",
        "from ps_service.graph_gateway import cypher\n",
        "def f(entries):\n    return apply_entries(entries)\n",
        "def f():\n    return FalkorDB(host='h', port=1)\n",
        "from ps_service.graph_gateway.connection import connect\n",
    ],
    ids=[
        "set_embedding",
        "create_fstring",
        "detach_delete",
        "set_in_module_constant",
        "driver_import",
        "applier_import",
        "cypher_import",
        "apply_entries_call",
        "construction",
        "write_capable_factory",
    ],
)
def test_the_guard_finds_a_planted_violation(source: str) -> None:
    assert len(scan_source(source)) >= 1


@pytest.mark.parametrize(
    "source",
    [
        '"""Writes n.embedding via SET; MERGE is described here, not run."""\nx = 1\n',
        'def f(g):\n    g.query("MATCH (n:Capability) WHERE n.embedding IS NULL RETURN n.id")\n',
        'def f(g):\n    g.query("CALL db.labels()")\n',
    ],
    ids=["prose", "missing_embedding_read", "labels_read"],
)
def test_the_guard_ignores_prose_and_reads(source: str) -> None:
    assert scan_source(source) == []


def test_write_cypher_is_found_even_beside_a_connection_helper_call() -> None:
    source = 'def f(g):\n    connect()\n    g.query("SET n.x = 1")\n'

    assert len(scan_source(source)) == 1
