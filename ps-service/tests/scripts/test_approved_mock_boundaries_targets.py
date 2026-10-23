"""Every `ps_service.mcp_interface.*` entry of the approved mock-boundary list names a live symbol.

`scripts/check_mock_boundaries.py` matches a patch target against the list by dotted suffix and
falls back to the bare name, so a stale entry (one that names a module that no longer defines the
symbol) still lets the checker pass. This test closes that gap for the MCP interface package: each
of its entries must import and resolve, so an entry that outlives the move of its symbol to another
module fails here.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "check_mock_boundaries.py"
_YAML = _REPO_ROOT / "docs" / "coding-standards" / "approved-mock-boundaries.yaml"
_PREFIX = "ps_service.mcp_interface."


def _load_yaml_entries() -> list[str]:
    spec = importlib.util.spec_from_file_location("_check_mock_boundaries_targets", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return sorted(module.load_boundary_entries(_YAML))


_ENTRIES = [entry for entry in _load_yaml_entries() if entry.startswith(_PREFIX)]


@pytest.mark.parametrize("entry", _ENTRIES)
def test_entry_resolves(entry: str) -> None:
    module_name, _, attribute = entry.rpartition(".")

    assert hasattr(importlib.import_module(module_name), attribute)


def test_entries_not_vacuous() -> None:
    assert len(_ENTRIES) >= 3
