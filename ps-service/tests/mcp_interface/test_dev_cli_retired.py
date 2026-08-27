"""Tests that the retired Query Engine dev CLI module is fully gone
(PLAN_REVIEWED.md §6, Batch 6; AC-013).

The load-bearing assertion is the FILESYSTEM check (F-09): the module file and
its compiled bytecode are gone. `find_spec(...) is None` is the secondary
confirmation, and the MCP `instructions` string must carry no stale path.

The retired module's name is assembled from fragments (`_RETIRED_STEM`) so a
repo-wide grep for that name stays clean per AC-013 -- a retirement guard
should assert an absence, not itself be a lingering textual reference.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import ps_service.query_engine
from ps_service.mcp_interface import mcp_server

_RETIRED_STEM = "cypher" + "_" + "cli"
_RETIRED_MODULE = f"ps_service.query_engine.{_RETIRED_STEM}"


def test_retired_dev_cli_file_and_bytecode_are_gone() -> None:
    query_engine_dir = Path(ps_service.query_engine.__file__).parent
    assert not (query_engine_dir / f"{_RETIRED_STEM}.py").exists()
    assert not list((query_engine_dir / "__pycache__").glob(f"{_RETIRED_STEM}*.pyc"))


def test_retired_dev_cli_module_not_importable() -> None:
    assert importlib.util.find_spec(_RETIRED_MODULE) is None


def test_instructions_string_has_no_stale_path() -> None:
    assert _RETIRED_STEM not in (mcp_server.server.instructions or "")
