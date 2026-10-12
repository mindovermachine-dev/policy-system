"""Export holds no write path to FalkorDB (issue #213, AC-BI-001, AC-BI-007).

Export's only graph write, the embedding backfill, goes through the Graph Write Gateway, so no
module under `ps_service.export` may open a FalkorDB connection of its own, name the gateway's
apply machinery, or pass write Cypher to a graph handle. The scan is
`graph_gateway.write_path_scan` (an AST walk of the literals passed to `query` and its siblings,
never docstrings or prose). The one connection-helper module (`falkordb_connection.py`, which
opens the READ handle) is exempt from the connection rule only; write Cypher is never admitted
anywhere. The REST orchestration (`api/export_orchestration.py`) and the maintainer CLI shim
(`tools/curated-export/export_instrument.py`, which opens the READ connection itself) are scanned
too: the three places Export is driven from.

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
_CONNECTION_HELPER = "falkordb_connection.py"
# `serialize.py` hides the gateway's replay-progress sentinel node from an artifact. It imports the
# label (a plain string constant) and nothing else of `graph_gateway.cypher`: no template, no
# applier. Exactly this one import is admitted.
_READ_SIDE_IMPORTS = {
    "serialize.py": frozenset({"ps_service.graph_gateway.cypher.REPLAY_STATE_LABEL"}),
}
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
    allow = path.name == _CONNECTION_HELPER
    findings = scan_source(
        path.read_text(encoding="utf-8"),
        allow_connection=allow,
        allow_imports=_READ_SIDE_IMPORTS.get(path.name, frozenset()),
    )
    assert findings == []


def test_only_the_connection_helper_needs_the_connection_allowance() -> None:
    needing = [
        path.name
        for path in _modules()
        if scan_source(
            path.read_text(encoding="utf-8"),
            allow_imports=_READ_SIDE_IMPORTS.get(path.name, frozenset()),
        )
        != []
    ]

    assert needing == [_CONNECTION_HELPER]


def test_serialize_may_import_only_the_replay_label_from_the_gateway_cypher() -> None:
    allowed = _READ_SIDE_IMPORTS["serialize.py"]

    assert scan_source("from ps_service.graph_gateway.cypher import REPLAY_STATE_LABEL\n") != []
    assert (
        scan_source(
            "from ps_service.graph_gateway.cypher import REPLAY_STATE_LABEL\n",
            allow_imports=allowed,
        )
        == []
    )
    assert (
        scan_source(
            "from ps_service.graph_gateway.cypher import UPSERT_NODE_TEMPLATE\n",
            allow_imports=allowed,
        )
        != []
    )


def test_the_rest_orchestration_passes_no_write_cypher_and_names_no_apply_machinery() -> None:
    source = (_API / "export_orchestration.py").read_text(encoding="utf-8")

    # Its connection helpers (a function-local `connect_from_config` and a `TYPE_CHECKING`
    # `falkordb` import) open the READ handle; only the write paths are forbidden here.
    findings = [
        finding
        for finding in scan_source(source)
        if "write Cypher" in finding
        or "applier" in finding
        or "cypher" in finding
        or "apply" in finding
    ]
    assert findings == []


def test_the_cli_shim_passes_no_write_cypher_and_opens_only_its_read_connection() -> None:
    source = _CLI.read_text(encoding="utf-8")

    # The read connection (`FalkorDB(host, port)` / `select_graph`) is the CLI's own; it is the
    # one script-level composition root. No write Cypher, no apply machinery.
    assert scan_source(source, allow_connection=True) == []
    assert scan_source(source) != []  # it does open the connection, so the allowance is needed


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
        "def f(db):\n    return db.select_graph('x')\n",
        "def f(config):\n    return connect_from_config(config)\n",
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
        "select_graph",
        "connect",
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


def test_the_connection_allowance_never_admits_write_cypher() -> None:
    source = 'def f(g):\n    connect()\n    g.query("SET n.x = 1")\n'

    assert len(scan_source(source, allow_connection=True)) == 1
