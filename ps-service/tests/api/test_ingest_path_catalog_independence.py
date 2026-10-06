"""Static guard: the ingest path never references or reads the curated catalog (issue #193).

AC-BI-011/AC-BI-015 read as "no ingest-path code references or reads the catalog",
not "``catalog.json`` is never loaded in the process" -- ``change_check_orchestration``
still imports ``ps_service.api.catalog`` for the sweep's ``find_by_celex``. The guard
therefore scans only the three ingest-path modules, with two independent checks (CHANGES.md
M5): no forbidden catalog-lookup name appears anywhere in their code, and none of them
imports ``ps_service.api.catalog`` at all (even under ``TYPE_CHECKING``).

Docstrings and comments are not code, so the AST walk ignores them.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

_INGEST_PATH_MODULES = (
    "ps_service.api.ingestion_orchestration",
    "ps_service.api.routes",
    "ps_service.mcp_interface.mcp_server",
)
_FORBIDDEN_NAMES = frozenset(
    {
        "find_by_celex",
        "find_short_name_collision",
        "REGULATION_CATALOG",
        "ShortNameCuratedMismatchError",
        "validate_and_resolve_catalog_entry",
    }
)
_CATALOG_MODULE = "ps_service.api.catalog"


def _parse(module_name: str) -> ast.Module:
    module_file = importlib.import_module(module_name).__file__
    assert module_file is not None
    return ast.parse(Path(module_file).read_text(encoding="utf-8"))


def _referenced_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rpartition(".")[2])
    return names


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


@pytest.mark.parametrize("module_name", _INGEST_PATH_MODULES)
def test_ingest_path_modules_never_reference_catalog_lookups(module_name: str) -> None:
    """AC-BI-011/015: no catalog lookup, catalog constant or curated-mismatch error is named."""
    found = _referenced_names(_parse(module_name)) & _FORBIDDEN_NAMES

    assert found == set()


@pytest.mark.parametrize("module_name", _INGEST_PATH_MODULES)
def test_ingest_path_modules_never_import_the_catalog_module(module_name: str) -> None:
    """AC-BI-015: the ingest path does not even import ``ps_service.api.catalog``."""
    imported = _imported_modules(_parse(module_name))

    assert _CATALOG_MODULE not in imported
