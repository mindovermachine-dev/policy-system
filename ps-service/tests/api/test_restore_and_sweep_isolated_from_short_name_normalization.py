"""Static guard: restore and the sweep never use ingest-side short_name normalization (#193).

AC-BI-009/AC-BI-010. ``normalize_short_name`` upper-cases the ``short_name`` a caller
supplies on the ingest path (``RegulatoryInstrument.id`` becomes ``CRA-1.0``). FalkorDB
graph names are case-sensitive and stay lowercase (``cra_native``), so restore and the
sweep must keep deriving them from the lowercase canonical value. This is a
CHARACTERIZATION guard: it passes before and after the change and fails the moment a
restore or sweep module starts referencing the normalizer.

Docstrings and comments are not code, so the AST walk ignores them.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

_NORMALIZER_NAME = "normalize_short_name"
_RESTORE_AND_SWEEP_PACKAGES = [
    "ps_service.restore",
    "ps_service.change_monitor",
]
_RESTORE_AND_SWEEP_MODULES = [
    "ps_service.api.restore_orchestration",
    "ps_service.api.change_check_orchestration",
]


def _python_files(module_name: str) -> list[Path]:
    spec = importlib.util.find_spec(module_name)
    assert spec is not None
    assert spec.origin is not None
    origin = Path(spec.origin)
    if spec.submodule_search_locations is None:
        return [origin]
    return sorted(origin.parent.rglob("*.py"))


def _names_referenced_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rpartition(".")[2])
    return names


@pytest.mark.parametrize("module_name", [*_RESTORE_AND_SWEEP_PACKAGES, *_RESTORE_AND_SWEEP_MODULES])
def test_restore_and_sweep_modules_do_not_import_normalize_short_name(module_name: str) -> None:
    """AC-BI-009/010 (characterization): no restore or sweep file references the normalizer."""
    files = _python_files(module_name)
    assert files, f"no source files found for {module_name}"

    offenders = [str(path) for path in files if _NORMALIZER_NAME in _names_referenced_in(path)]

    assert offenders == []
