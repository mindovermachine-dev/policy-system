"""Graph Cleanup holds no write path to FalkorDB.

Every `policy_system` mutation goes through the Graph Write Gateway, so no module under
`ps_service.graph_cleanup` may open a FalkorDB connection of its own, name the gateway's apply
machinery, or pass write Cypher to a graph handle. The scan is an AST walk. It looks at the text
handed to a call named `query`, `ro_query`, `_execute_query`, `_read_query`, `read_rows` or
`query_rows`: a string literal, the constant parts of an f-string, or a name that resolves to such
text. It never looks at docstrings or prose, so a module may *describe* `MERGE` without being
flagged, and it matches the uppercase keywords only, so the lowercase `'merged'` of a read stays
clean.

Positive half: each of the five writers of `group_builders.py` reaches `submit_cleanup_group`, which
reaches the gateway sink `submit_group` (same-module call graphs by name, no mock), and each
executor reaches its writer.

Symbol absence: the former `graph_writer` module is gone and none of its statements
or functions is named in the source or the tests, outside the frozen reference.

The guard is itself guarded: planted violations must be found, and prose must not be.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
from graph_gateway.write_path_scan import calls_by_function, reaches, scan_source

import ps_service.graph_cleanup as graph_cleanup_package

_ROOT = Path(graph_cleanup_package.__file__).parent
_TESTS = Path(__file__).parent

_SINK = "submit_group"
_CLEANUP_SINK = "submit_cleanup_group"
_WRITERS = (
    "write_release",
    "write_capability_merge",
    "write_obligation_merge",
    "write_capability_unmerge",
    "write_obligation_unmerge",
    "write_role_merge",
    "write_role_unmerge",
)
_EXECUTOR_TO_WRITER = {
    "_write_after_audit": "write_capability_merge",
    "_write_obligation_after_audit": "write_obligation_merge",
    "_write_release_after_audit": "write_release",
    "_write_role_after_audit": "write_role_merge",
    "_write_capability_unmerge": "write_capability_unmerge",
    "_write_obligation_unmerge": "write_obligation_unmerge",
    "_write_role_unmerge": "write_role_unmerge",
}
_REMOVED_SYMBOLS = re.compile(
    r"graph_cleanup\.graph_writer"
    r"|\b(?:MERGE_CAPABILITIES|MERGE_OBLIGATIONS|RELEASE_GOVERNANCE|UNMERGE_CAPABILITY"
    r"|UNMERGE_OBLIGATION)_QUERY\b"
    r"|(?<![A-Za-z_])(?:merge_capabilities|merge_obligations|release_governance"
    r"|unmerge_capability|unmerge_obligation)\("
)
_FROZEN_REFERENCE = "_legacy_reference.py"
_THIS_FILE = Path(__file__).name


def _graph_cleanup_modules() -> list[Path]:
    return sorted(path for path in _ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def _module_param(path: Path) -> object:
    return pytest.param(path, id=path.relative_to(_ROOT).as_posix())


def test_the_scan_covers_the_graph_cleanup_package() -> None:
    names = {path.relative_to(_ROOT).as_posix() for path in _graph_cleanup_modules()}

    assert {
        "group_builders.py",
        "graph_submission.py",
        "graph_reader.py",
        "executors.py",
        "service.py",
    } <= names


@pytest.mark.parametrize("path", [_module_param(path) for path in _graph_cleanup_modules()])
def test_no_graph_cleanup_module_opens_a_write_connection_or_passes_write_cypher(
    path: Path,
) -> None:
    assert scan_source(path.read_text(encoding="utf-8")) == []


def test_write_cypher_is_found_even_beside_a_connection_helper_call() -> None:
    source = 'def f(g):\n    connect()\n    g.query("MERGE (n)")\n'

    assert len(scan_source(source)) == 1


def test_every_writer_reaches_the_cleanup_sink() -> None:
    source = (_ROOT / "group_builders.py").read_text(encoding="utf-8")

    assert [w for w in _WRITERS if not reaches(source, w, _CLEANUP_SINK)] == []


def test_the_cleanup_sink_reaches_the_gateway_sink() -> None:
    source = (_ROOT / "graph_submission.py").read_text(encoding="utf-8")

    assert reaches(source, _CLEANUP_SINK, _SINK)


def test_the_executors_call_the_writers() -> None:
    executors = (_ROOT / "executors.py").read_text(encoding="utf-8")
    calls = calls_by_function(executors)

    assert [name for name in _EXECUTOR_TO_WRITER if name not in calls] == []
    called: set[str] = set()
    for name in _EXECUTOR_TO_WRITER:
        called |= calls[name]
    assert set(_WRITERS) <= called


# --- the direct-write module and its symbols are gone ---


def test_the_direct_write_module_is_not_importable() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ps_service.graph_cleanup.graph_writer")
    assert not (_ROOT / "graph_writer.py").exists()


def test_no_removed_symbol_is_named_in_the_source_or_the_tests() -> None:
    files = [
        *_graph_cleanup_modules(),
        *(p for p in _TESTS.rglob("*.py") if p.name not in {_FROZEN_REFERENCE, _THIS_FILE}),
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
        "from ps_service.graph_cleanup.graph_writer import merge_capabilities",
        "q = MERGE_CAPABILITIES_QUERY",
        "x = unmerge_obligation(graph)",
        "release_governance(g)",
    ],
)
def test_the_symbol_scan_finds_a_planted_reference(text: str) -> None:
    assert _REMOVED_SYMBOLS.search(text)


@pytest.mark.parametrize(
    "text",
    [
        "execute_release_governance(x)",
        "preview_release_governance(x)",
        "plan_release_governance(x)",
        "validate_release_governance(x)",
        "LEGACY_MERGE_CAPABILITIES_QUERY",
    ],
)
def test_the_symbol_scan_ignores_the_lookalikes(text: str) -> None:
    assert _REMOVED_SYMBOLS.search(text) is None


# --- the guard of the guard: it must fail on a planted violation, and only on one ---


@pytest.mark.parametrize(
    "source",
    [
        'def f(g):\n    g.query("MERGE (n:Policy {id: $id})", {"id": 1})\n',
        'def f(g, label):\n    g.ro_query(f"CREATE (n:{label})")\n',
        'def f(g):\n    g._execute_query(query="MATCH (n) DETACH DELETE n")\n',
        'def f(g):\n    _read_query(g, "MATCH (n) SET n.x = 1")\n',
        'def f(g):\n    read_rows(g, "MATCH (n) DELETE n")\n',
        'def f(g):\n    query_rows(g, "MATCH (n) FOREACH (x IN [1] | SET n.x = x)")\n',
        'Q = "MATCH (n) SET n.x = 1"\ndef f(g):\n    g.query(Q)\n',
        'Q = "CREATE (n:{kind})"\ndef f(g):\n    g.query(Q.format(kind="A"))\n',
        'Q = "MATCH (n) DELETE n"\ndef f(g):\n    q = Q.format()\n    g.query(q)\n',
        'def f(g):\n    q = "MATCH (n) REMOVE n.x"\n    g.query(q)\n',
        'A = "MATCH (n) "\nB = "DELETE n"\ndef f(g):\n    g.query(A + B)\n',
        "from falkordb import FalkorDB\n",
        "from ps_service.graph_gateway.applier import apply_entries\n",
        "from ps_service.graph_gateway import cypher\n",
        "def f(entries):\n    return apply_entries(entries)\n",
        "def f():\n    return FalkorDB(host='h', port=1)\n",
        "from ps_service.graph_gateway.connection import connect\n",
    ],
    ids=[
        "merge",
        "create_fstring",
        "detach_delete",
        "set_via_read_helper",
        "delete_via_read_rows",
        "foreach_via_query_rows",
        "set_in_module_constant",
        "create_in_formatted_constant",
        "delete_in_local_from_formatted_constant",
        "remove_in_local",
        "delete_in_concatenation",
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
        'def f(g, label):\n    g.ro_query(f"MATCH (n:{label}) RETURN n.id")\n',
        (
            "Q = \"MATCH (r:PendingReview) WHERE r.status <> 'merged' RETURN r.id\"\n"
            "def f(g):\n    g.query(Q)\n"
        ),
        "T = \"MATCH (n:{kind}) RETURN n.id\"\ndef f(g):\n    g.query(T.format(kind='A'))\n",
    ],
    ids=[
        "docstring",
        "comment_and_assignment",
        "read_query",
        "variable_query",
        "message_only",
        "read_fstring_label",
        "lowercase_merged_in_a_constant",
        "read_template_formatted",
    ],
)
def test_the_guard_ignores_prose_and_read_queries(source: str) -> None:
    assert scan_source(source) == []


def test_the_sink_check_fails_for_a_writer_that_never_submits_and_passes_through_a_helper() -> None:
    sink = "submit_group"
    assert reaches("def persist_x(t):\n    return 1\n", "persist_x", sink) is False
    assert (
        reaches("def persist_x(g):\n    g.query('MATCH (n) RETURN n')\n", "persist_x", sink)
        is False
    )
    assert reaches("def persist_x(t):\n    submit_group(t, ())\n", "persist_x", sink) is True
    assert (
        reaches(
            "def persist_x(t):\n    _go(t)\ndef _go(t):\n    submit_group(t, ())\n",
            "persist_x",
            sink,
        )
        is True
    )


def test_a_planted_redis_driver_import_is_found_and_redis_exceptions_is_not() -> None:
    assert len(scan_source("from redis import Redis\n")) >= 1
    assert scan_source("import redis.exceptions\n") == []
