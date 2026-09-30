"""AC-BI-009 (issue #130): `curated_source` has no FalkorDB/`redis` dependency of its own.

The override moved to the PS state Postgres (`runtime_config`); `GET /catalog` therefore needs
no graph handle. Checked structurally on the real source tree: no import of, and no token
naming, a FalkorDB/`redis` client, a graph handle, Company Merge or the retired
`CatalogSourceOverride` node.
"""

from __future__ import annotations

import ast
from pathlib import Path

import ps_service.curated_source as curated_source_package

_FORBIDDEN_IMPORT_ROOTS = ("redis", "falkordb", "ps_service.company_merge")
_FORBIDDEN_TOKENS = ("GraphHandle", "CatalogSourceOverride", "FALKORDB", "falkordb")


def _sources() -> list[Path]:
    package_file = curated_source_package.__file__
    assert package_file is not None
    return sorted(Path(package_file).parent.rglob("*.py"))


def _imported_modules(source: Path) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
    return modules


def test_curated_source_imports_no_redis_falkordb_or_graph_client() -> None:
    offenders = [
        f"{source.name}: {module}"
        for source in _sources()
        for module in _imported_modules(source)
        if module.split(".")[0] in {"redis", "falkordb"}
        or module.startswith("ps_service.company_merge")
    ]

    assert offenders == []


def test_curated_source_source_names_no_graph_handle_or_override_node() -> None:
    offenders = [
        f"{source.name}: {token}"
        for source in _sources()
        for token in _FORBIDDEN_TOKENS
        if token in source.read_text(encoding="utf-8")
    ]

    assert offenders == []
