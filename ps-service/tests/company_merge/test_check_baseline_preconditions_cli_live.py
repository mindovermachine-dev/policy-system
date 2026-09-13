"""Live regression test for `tools/company-merge/check_baseline_preconditions.py`
(issue #28, PLAN.md §2.2).

`@pytest.mark.falkordb_live`, strictly read-only, no approval needed -- runs
the real CLI's `main()` (no `graph_provider` override, so it makes a real
`PS_FALKORDB_HOST`/`PS_FALKORDB_PORT`-driven connection) against real
FalkorDB, asserting exit `0` right now -- matches BASELINE.md's/
`test_preconditions_live.py`'s own finding of 0 `HAS`-edge-cardinality
violations across all three currently-mapped baseline graphs (`cra_baseline`,
`gdpr_baseline`, `nis2_baseline`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "tools"
    / "company-merge"
    / "check_baseline_preconditions.py"
)
_MODULE_NAME = "_check_baseline_preconditions_cli_under_test_live"


def _load_cli_module() -> ModuleType:
    """Import the CLI shim fresh, by file path -- see the fake-wiring test's
    `_load_cli_module` docstring for why `sys.modules` registration is
    required before `exec_module` (the CLI's local frozen/slotted dataclass).
    """
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[_MODULE_NAME]
        raise
    return module


@pytest.mark.falkordb_live
def test_real_baseline_graphs_pass_the_precondition_check_right_now() -> None:
    cli_module = _load_cli_module()

    exit_code = cli_module.main([])

    assert exit_code == 0
