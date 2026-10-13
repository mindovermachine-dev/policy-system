"""Company Merge holds no write path to FalkorDB (issue #210, AC-BI-001).

Every `policy_system` and `{short}_baseline` mutation goes through the Graph Write Gateway, so no
module under `ps_service.company_merge` may open a FalkorDB connection of its own, name the
gateway's apply machinery, or pass write Cypher to a graph handle. The scan is an AST walk. It
looks at the text handed to a call named `query`, `ro_query`, `_execute_query` or `_read_query`
(and to `read_rows`, the read wrapper): a string literal, the constant parts of an f-string, or a
name that resolves to such text (Company Merge keeps its read Cypher in constants such as
`_LIST_PENDING_REVIEWS_QUERY`, and builds some with `TEMPLATE.format(...)`). It never looks at
docstrings or prose, so a module may *describe* `MERGE` without being flagged, and it matches the
uppercase keywords only, so the lowercase `'merged'` of a read stays clean. The one module
allowed to touch the FalkorDB driver is the gateway package; `falkordb_client.py` is a read-only
delegate scanned like every other module.

Positive half: every public writer (`persist_*`, `backfill_*`, `resolve_review`) reaches the
gateway sink `submit_group`, found by a same-module call graph by name (no mock).

The guard is itself guarded: planted violations must be found, and prose must not be.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from graph_gateway.write_path_scan import called_name, scan_source

import ps_service.company_merge as company_merge_package

_ROOT = Path(company_merge_package.__file__).parent
_SINK = "submit_group"
_WRITER_MODULES = ("graph_writer.py", "pending_review.py")


def reaches_the_sink(source: str, function: str) -> bool:
    """Whether `function` calls `submit_group`, directly or through same-module functions."""
    tree = ast.parse(source)
    calls: dict[str, set[str]] = {
        node.name: {
            name
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            if (name := called_name(call))
        }
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    seen: set[str] = set()
    pending = [function]
    while pending:
        current = pending.pop()
        if current in seen or current not in calls:
            continue
        seen.add(current)
        if _SINK in calls[current]:
            return True
        pending.extend(calls[current])
    return False


def _public_writers(source: str) -> list[str]:
    return [
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef)
        and (node.name.startswith(("persist_", "backfill_")) or node.name == "resolve_review")
    ]


def _company_merge_modules() -> list[Path]:
    return sorted(path for path in _ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def test_the_scan_covers_the_company_merge_package() -> None:
    names = {path.relative_to(_ROOT).as_posix() for path in _company_merge_modules()}

    assert {
        "graph_writer.py",
        "pending_review.py",
        "merge.py",
        "dedup.py",
        "graph_reader.py",
        "obligation_redirect.py",
        "falkordb_client.py",
        "governance_import.py",
    } <= names


@pytest.mark.parametrize(
    "path", _company_merge_modules(), ids=lambda path: path.relative_to(_ROOT).as_posix()
)
def test_no_company_merge_module_opens_a_write_connection_or_passes_write_cypher(
    path: Path,
) -> None:
    assert scan_source(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("module", _WRITER_MODULES)
def test_every_public_writer_reaches_the_gateway_sink(module: str) -> None:
    source = (_ROOT / module).read_text(encoding="utf-8")
    writers = _public_writers(source)

    assert writers != []
    assert [w for w in writers if not reaches_the_sink(source, w)] == []


def test_the_writers_found_include_every_single_tenant_and_baseline_embedding_writer() -> None:
    found = {
        writer
        for module in _WRITER_MODULES
        for writer in _public_writers((_ROOT / module).read_text(encoding="utf-8"))
    }

    assert {
        "persist_role_and_requirement_passthrough",
        "persist_obligation_passthrough",
        "persist_governance_drafts",
        "persist_practice_area_and_risk_path_passthrough",
        "persist_canonical_nodes",
        "backfill_canonical_embeddings",
        "backfill_incoming_capability_embeddings",
        "persist_rewired_edges",
        "persist_pending_reviews",
        "resolve_review",
    } <= found


# --- the guard of the guard: it must fail on a planted violation, and only on one ---


@pytest.mark.parametrize(
    "source",
    [
        'def f(g):\n    g.query("MERGE (n:Policy {id: $id})", {"id": 1})\n',
        'def f(g, label):\n    g.ro_query(f"CREATE (n:{label})")\n',
        'def f(g):\n    g._execute_query(query="MATCH (n) DETACH DELETE n")\n',
        'def f(g):\n    _read_query(g, "MATCH (n) SET n.x = 1")\n',
        'def f(g):\n    read_rows(g, "MATCH (n) DELETE n")\n',
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


def test_the_former_connection_helper_gets_no_allowance() -> None:
    helper = "from falkordb import FalkorDB\ndef connect(host):\n    return FalkorDB(host=host)\n"

    assert len(scan_source(helper)) >= 2


def test_the_sink_check_fails_for_a_writer_that_never_submits_and_passes_through_a_helper() -> None:
    assert reaches_the_sink("def persist_x(t):\n    return 1\n", "persist_x") is False
    assert (
        reaches_the_sink("def persist_x(g):\n    g.query('MATCH (n) RETURN n')\n", "persist_x")
        is False
    )
    assert reaches_the_sink("def persist_x(t):\n    submit_group(t, ())\n", "persist_x") is True
    assert (
        reaches_the_sink(
            "def persist_x(t):\n    _go(t)\ndef _go(t):\n    submit_group(t, ())\n", "persist_x"
        )
        is True
    )
