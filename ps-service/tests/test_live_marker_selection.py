"""A `postgres_live` run must stay green without FalkorDB or an LLM provider (issue #205).

`-m postgres_live` is the documented way to run the live Postgres tests against a scratch server
(see `tests/persistence/provisioned_postgres.py`). A test that also needs
FalkorDB and an LLM provider carries those markers (the repo convention: every external service a
test needs is a marker on it, e.g. `cellar_live` + `falkordb_live`) and is selected by the
conjunction, not by `postgres_live` alone. The real-socket startup test spawns the whole service,
so `/ready` 200 needs state Postgres (provisioned through a fixture), FalkorDB and the LLM
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
_COLLECT_TIMEOUT_SECONDS = 120


def _collected(marker_expression: str) -> str:
    result = subprocess.run(  # noqa: S603 -- fixed argv, our own interpreter, no shell
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", marker_expression, _FILE],
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
