"""A `postgres_live` run must stay green without FalkorDB or an LLM provider.

`-m postgres_live` is the documented way to run the live Postgres tests against a scratch server
(see `tests/persistence/provisioned_postgres.py`). A test that also needs
FalkorDB and an LLM provider carries those markers (the repo convention: every external service a
test needs is a marker on it, e.g. `cellar_live` + `falkordb_live`) and is selected by the
conjunction, not by `postgres_live` alone. The real-socket startup test spawns the whole service,
so `/readyz` 200 needs state Postgres (provisioned through a fixture), FalkorDB and the LLM
Interface; it must not ride on the `postgres_live` marker.

Placement: asserts facts about test selection, like `test_upgrade_scripts_wait_for_jobs.py`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_FILE = "ps-service/tests/test_main_integration.py"
_TEST = "test_mcp_streamable_http_transport_reachable_over_a_real_socket"
_ENGPRAC_FILE = "ps-service/tests/export/test_engineering_practices_migration_live.py"
_ENGPRAC_SHAPE_TEST = (
    "test_engprac_baseline_passes_company_merges_own_baseline_graph_shape_assertions"
)
_FALKORDB_WITHOUT_POSTGRES = (
    "falkordb_live and not postgres_live and not llm_live and not cellar_live and not integration"
)
_COLLECT_TIMEOUT_SECONDS = 120


def _collected(marker_expression: str, target: str = _FILE) -> str:
    result = subprocess.run(  # noqa: S603 -- fixed argv, our own interpreter, no shell
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", marker_expression, target],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=_COLLECT_TIMEOUT_SECONDS,
    )
    return result.stdout


def test_postgres_live_selection_excludes_the_test_that_needs_falkordb_and_an_llm() -> None:
    assert _TEST not in _collected("postgres_live")


def test_real_socket_startup_test_is_selected_by_all_of_its_service_markers() -> None:
    assert _TEST in _collected("integration and falkordb_live and llm_live")


def test_falkordb_selection_without_postgres_excludes_the_seeded_graph_shape_test() -> None:
    """The shape test seeds through a scratch Postgres, so a FalkorDB-only run must skip it."""
    assert _ENGPRAC_SHAPE_TEST not in _collected(_FALKORDB_WITHOUT_POSTGRES, _ENGPRAC_FILE)


def test_postgres_live_selection_includes_the_seeded_graph_shape_test() -> None:
    assert _ENGPRAC_SHAPE_TEST in _collected("falkordb_live and postgres_live", _ENGPRAC_FILE)


def test_collecting_the_whole_tree_finds_no_postgres_test_missing_its_marker() -> None:
    """A test whose fixtures provision Postgres but lacks `postgres_live` aborts collection."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            "falkordb_live",
            "ps-service/tests",
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=_COLLECT_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
