"""Regulatory Change Monitor holds no write path to FalkorDB (issue #213, AC-BI-001, AC-BI-007).

Every succession mutation goes through the Graph Write Gateway, so no module under
`ps_service.change_monitor` may open a FalkorDB connection of its own, name the gateway's apply
machinery, or pass write Cypher to a graph handle. The scan is `graph_gateway.write_path_scan`
(an AST walk of the literals passed to `query` and its siblings, never docstrings or prose). The
one connection-helper module (`falkordb_client.py`, which opens the READ handle) is exempt from
the connection rule only; write Cypher is never admitted anywhere.

Positive half: each of the five succession writers reaches `submit_succession_group`, which
reaches the gateway sink `submit_group` through `submit_or_raise` (same-module call graphs by
name, no mock).

Symbol absence (AC-BI-001): the five pre-#213 write constants are named neither in the source nor
in the tests, outside the frozen reference.

The guard is itself guarded: planted violations must be found, and prose must not be.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from graph_gateway.write_path_scan import reaches, scan_source

import ps_service.change_monitor as change_monitor_package

_ROOT = Path(change_monitor_package.__file__).parent
_TESTS = Path(__file__).parent
_CONNECTION_HELPER = "falkordb_client.py"
_WRITERS = (
    "set_new_version_property",
    "mark_stage_complete",
    "link_and_supersede",
    "supersede_in_single_tenant",
    "clear_marker",
)
_SUCCESSION_SINK = "submit_succession_group"
_GATEWAY_SINK = "submit_or_raise"
_REMOVED_SYMBOLS = re.compile(
    r"\b(?:_SET_VERSION_QUERY|_MARK_STAGE_QUERY|_FUSED_SUCCESSION_QUERY"
    r"|_SINGLE_TENANT_SUPERSEDE_QUERY|_CLEAR_MARKER_QUERY)\b"
)
_FROZEN_REFERENCE = "_legacy_reference.py"
_THIS_FILE = Path(__file__).name


def _modules() -> list[Path]:
    return sorted(path for path in _ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def _module_param(path: Path) -> object:
    return pytest.param(path, id=path.relative_to(_ROOT).as_posix())


def test_the_scan_covers_the_change_monitor_package() -> None:
    names = {path.relative_to(_ROOT).as_posix() for path in _modules()}

    assert {
        "succession.py",
        "graph_submission.py",
        "trigger.py",
        "graph_reader.py",
        "poll.py",
        "falkordb_client.py",
    } <= names


@pytest.mark.parametrize("path", [_module_param(path) for path in _modules()])
def test_no_change_monitor_module_opens_a_write_connection_or_passes_write_cypher(
    path: Path,
) -> None:
    allow = path.name == _CONNECTION_HELPER
    assert scan_source(path.read_text(encoding="utf-8"), allow_connection=allow) == []


def test_only_the_connection_helper_needs_the_connection_allowance() -> None:
    needing = [
        path.name for path in _modules() if scan_source(path.read_text(encoding="utf-8")) != []
    ]

    assert needing == [_CONNECTION_HELPER]


def test_the_connection_allowance_never_admits_write_cypher() -> None:
    source = 'def f(g):\n    connect()\n    g.query("MERGE (n)")\n'

    assert len(scan_source(source, allow_connection=True)) == 1


def test_every_succession_writer_reaches_the_succession_sink() -> None:
    source = (_ROOT / "succession.py").read_text(encoding="utf-8")

    assert [w for w in _WRITERS if not reaches(source, w, _SUCCESSION_SINK)] == []


def test_the_succession_sink_reaches_the_gateway_sink() -> None:
    source = (_ROOT / "graph_submission.py").read_text(encoding="utf-8")

    assert reaches(source, _SUCCESSION_SINK, _GATEWAY_SINK)


def test_the_orchestrator_calls_every_writer() -> None:
    trigger = (_ROOT / "trigger.py").read_text(encoding="utf-8")

    assert [w for w in _WRITERS if f"{w}(" not in trigger] == []


# --- AC-BI-001: the direct writers' symbols are gone ---


def test_no_removed_symbol_is_named_in_the_source_or_the_tests() -> None:
    files = [
        *_modules(),
        *(p for p in _TESTS.rglob("*.py") if p.name not in {_FROZEN_REFERENCE, _THIS_FILE}),
        *(p for p in _TESTS.parent.glob("api/*.py")),
    ]

    hits = [
        f"{path.name}: {match.group(0)}"
        for path in files
        for match in _REMOVED_SYMBOLS.finditer(path.read_text(encoding="utf-8"))
    ]

    assert hits == []


@pytest.mark.parametrize(
    "text",
    [
        "q = _SET_VERSION_QUERY",
        "_MARK_STAGE_QUERY",
        "x(_FUSED_SUCCESSION_QUERY)",
        "_CLEAR_MARKER_QUERY",
    ],
)
def test_the_symbol_scan_finds_a_planted_reference(text: str) -> None:
    assert _REMOVED_SYMBOLS.search(text)


def test_the_symbol_scan_ignores_the_read_queries() -> None:
    assert _REMOVED_SYMBOLS.search("_FIND_PRIOR_QUERY _REINGESTION_FACTS_QUERY") is None


# --- the guard of the guard: it must fail on a planted violation, and only on one ---


@pytest.mark.parametrize(
    "source",
    [
        'def f(g):\n    g.query("MERGE (n:Policy {id: $id})", {"id": 1})\n',
        'def f(g, label):\n    g.ro_query(f"CREATE (n:{label})")\n',
        'def f(g):\n    g._execute_query(query="MATCH (n) DETACH DELETE n")\n',
        'def f(g):\n    _read_query(g, "MATCH (n) SET n.x = 1")\n',
        'Q = "MATCH (n) SET n.x = 1"\ndef f(g):\n    g.query(Q)\n',
        'Q = "CREATE (n:{kind})"\ndef f(g):\n    g.query(Q.format(kind="A"))\n',
        'A = "MATCH (n) "\nB = "DELETE n"\ndef f(g):\n    g.query(A + B)\n',
        "from falkordb import FalkorDB\n",
        "from ps_service.graph_gateway.applier import apply_entries\n",
        "from ps_service.graph_gateway import cypher\n",
        "def f(entries):\n    return apply_entries(entries)\n",
        "def f(db):\n    return db.select_graph('x')\n",
        "def f(config):\n    return connect_from_config(config)\n",
        "from redis import Redis\n",
    ],
    ids=[
        "merge",
        "create_fstring",
        "detach_delete",
        "set_via_read_helper",
        "set_in_module_constant",
        "create_in_formatted_constant",
        "delete_in_concatenation",
        "driver_import",
        "applier_import",
        "cypher_import",
        "apply_entries_call",
        "select_graph",
        "connect",
        "redis_client_import",
    ],
)
def test_the_guard_finds_a_planted_violation(source: str) -> None:
    assert len(scan_source(source)) >= 1


@pytest.mark.parametrize(
    "source",
    [
        "from ps_service.graph_gateway.applier import apply_entries\n",
        "def f(entries):\n    return apply_entries(entries)\n",
        "from ps_service.graph_gateway import cypher\n",
    ],
)
def test_the_connection_allowance_never_admits_the_gateway_apply_machinery(source: str) -> None:
    assert len(scan_source(source, allow_connection=True)) >= 1


@pytest.mark.parametrize(
    "source",
    [
        'def f():\n    """MERGE and CREATE live in the gateway, not here: SET DELETE."""\n',
        '# MERGE (n)\nX = "MERGE (n)"\n',
        'def f(g):\n    g.query("MATCH (n:Policy) WHERE n.id IN $ids RETURN n.id", {"ids": []})\n',
        "def f(g, q):\n    g.query(q)\n",
        'def f():\n    raise ValueError("cannot SET the property")\n',
        "import redis.exceptions\n",
        "Q = \"MATCH (n) WHERE n.status = 'merged' RETURN n.id\"\ndef f(g):\n    g.query(Q)\n",
    ],
    ids=[
        "docstring",
        "comment_and_assignment",
        "read_query",
        "variable_query",
        "message_only",
        "redis_exceptions_import",
        "lowercase_merged_in_a_constant",
    ],
)
def test_the_guard_ignores_prose_and_read_queries(source: str) -> None:
    assert scan_source(source) == []


def test_the_sink_check_fails_for_a_writer_that_never_submits_and_passes_through_a_helper() -> None:
    assert reaches("def w(t):\n    return 1\n", "w", _SUCCESSION_SINK) is False
    assert reaches("def w(g):\n    g.query('MATCH (n) RETURN n')\n", "w", _SUCCESSION_SINK) is False
    assert reaches("def w(t):\n    submit_succession_group(t)\n", "w", _SUCCESSION_SINK) is True
    helper = "def w(t):\n    _go(t)\ndef _go(t):\n    submit_succession_group(t)\n"
    assert reaches(helper, "w", _SUCCESSION_SINK) is True
