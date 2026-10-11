"""Restore writes its offline `GRAPH.COPY` snapshot with its own interim writer (issue #210, D1).

Company Merge's writers move onto the Graph Write Gateway in #210. Restore's
baseline merge targets a snapshot graph that is later swapped in by a
WATCH-guarded RENAME, which the gateway cannot log until #214, so Restore keeps
a verbatim copy of the pre-migration Cypher writers in
`restore/snapshot_merge_writer.py`. This test pins the decoupling: no module of
the `restore` package may import `ps_service.company_merge.graph_writer`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import ps_service.restore as restore_package

_FORBIDDEN_MODULE = "ps_service.company_merge.graph_writer"


def _imports_graph_writer(tree: ast.AST) -> list[str]:
    offences: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offences.extend(alias.name for alias in node.names if alias.name == _FORBIDDEN_MODULE)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == _FORBIDDEN_MODULE:
                offences.append(module)
            elif module == "ps_service.company_merge" and any(
                alias.name == "graph_writer" for alias in node.names
            ):
                offences.append(f"{module}.graph_writer")
    return offences


def test_no_restore_module_imports_the_company_merge_graph_writer() -> None:
    package_dir = Path(restore_package.__file__).parent
    modules = sorted(package_dir.glob("*.py"))
    assert package_dir / "restore_instrument.py" in modules  # the scan covers the consumer

    offences = {
        module.name: found
        for module in modules
        if (found := _imports_graph_writer(ast.parse(module.read_text(encoding="utf-8"))))
    }

    assert offences == {}


def test_the_scan_detects_both_import_spellings() -> None:
    """Guard of the guard: a planted import of either shape is reported."""
    plain = ast.parse("import ps_service.company_merge.graph_writer")
    from_module = ast.parse(
        "from ps_service.company_merge.graph_writer import persist_rewired_edges"
    )
    from_package = ast.parse("from ps_service.company_merge import graph_reader, graph_writer")
    clean = ast.parse("from ps_service.company_merge import graph_reader")

    assert _imports_graph_writer(plain) == [_FORBIDDEN_MODULE]
    assert _imports_graph_writer(from_module) == [_FORBIDDEN_MODULE]
    assert _imports_graph_writer(from_package) == [f"{_FORBIDDEN_MODULE}"]
    assert _imports_graph_writer(clean) == []
