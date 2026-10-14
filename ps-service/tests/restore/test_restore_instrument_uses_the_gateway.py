"""`restore_instrument` writes only through the Graph Write Gateway.

The legacy staging modules are gone; nothing in the restore path references them: no `GRAPH.COPY`,
no `RENAME`, no `WATCH`, no write Cypher. The baseline merge's behaviour lives in
`merge_primitives.build_policy_system_leg` (pinned by `test_merge_primitives.py`).
"""

from __future__ import annotations

import ast
import importlib
import inspect

import pytest

import ps_service.restore.restore_instrument as restore_instrument_module

_LEGACY_MODULES = (
    "ps_service.restore.staging",
    "ps_service.restore.populate",
    "ps_service.restore.snapshot_merge_writer",
    "ps_service.export.falkordb_connection",
    "redis",
)


def _imported_modules() -> set[str]:
    tree = ast.parse(inspect.getsource(restore_instrument_module))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def test_restore_instrument_imports_none_of_the_legacy_staging_modules() -> None:
    imported = _imported_modules()

    for legacy in _LEGACY_MODULES:
        assert not any(name == legacy or name.startswith(f"{legacy}.") for name in imported), legacy


def test_the_pre_gateway_offline_merge_function_is_gone() -> None:
    assert not hasattr(restore_instrument_module, "_run_baseline_merge")


def test_restore_instrument_issues_no_cypher() -> None:
    source = inspect.getsource(restore_instrument_module)

    for keyword in ("MERGE (", "CREATE (", "GRAPH.COPY", ".rename(", ".watch("):
        assert keyword not in source, keyword


def test_the_deleted_legacy_modules_are_gone() -> None:
    for name in ("staging", "populate", "snapshot_merge_writer"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"ps_service.restore.{name}")
