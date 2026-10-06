"""Proves `serialize_live_markers` keeps live marker groups out of xdist (issue #197).

Red-before-green: the plugin does not exist before this slice. `addopts` in the root
`pyproject.toml` turns xdist on for every invocation, including `-m falkordb_live`, whose
tests share one external FalkorDB graph and are not safe to run concurrently. The plugin forces
a single process whenever `-m` selects a live-marked test and no `-n` was passed explicitly.

Drives a real `python -m pytest` subprocess against a throwaway project in `tmp_path` -- no
monkeypatching -- and reads whether xdist workers ran from pytest's own verbose output
(`[gw0]` prefixes appear only when a worker executed the test).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ps_test_support.serialize_live_markers import LIVE_MARKERS

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
SUBPROCESS_TIMEOUT_SECONDS = 120.0
PLUGIN_MODULE = "ps_test_support.serialize_live_markers"
DEFAULT_EXPRESSION = " and ".join(f"not {marker}" for marker in LIVE_MARKERS)

_PROJECT_TEST_FILE = """\
import pytest


@pytest.mark.falkordb_live
def test_live():
    pass


def test_hermetic():
    pass
"""


def _write_project(project: Path) -> None:
    markers = "\n".join(f"    {marker}: live group" for marker in LIVE_MARKERS)
    (project / "pytest.ini").write_text(
        f'[pytest]\naddopts = -n 2 -p {PLUGIN_MODULE} -m "{DEFAULT_EXPRESSION}"\n'
        f"markers =\n{markers}\n",
        encoding="utf-8",
    )
    (project / "test_sample.py").write_text(_PROJECT_TEST_FILE, encoding="utf-8")


def _run_pytest(project: Path, args: Sequence[str]) -> str:
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_ADDOPTS"}
    completed = subprocess.run(  # noqa: S603 - fixed argv built from this module's own constants
        [sys.executable, "-m", "pytest", "-v", "-p", "no:cacheprovider", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stdout


def _ran_on_xdist_workers(output: str) -> bool:
    return re.search(r"\[gw\d+\]", output) is not None


@pytest.fixture(name="project")
def _project(  # pyright: ignore[reportUnusedFunction]  # pytest fixture, resolved by name
    tmp_path: Path,
) -> Path:
    _write_project(tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    ("args", "expect_workers"),
    [
        pytest.param([], True, id="default-expression-stays-parallel"),
        pytest.param(["-m", "falkordb_live"], False, id="live-marker-forced-serial"),
        pytest.param(
            ["-m", "falkordb_live", "-n", "2"], True, id="explicit-n-overrides-the-serial-force"
        ),
        pytest.param(["-m", "falkordb_live", "-n0"], False, id="explicit-n0-stays-serial"),
        pytest.param(["-n0"], False, id="explicit-n0-without-marker-is-serial"),
        pytest.param(
            ["-m", "not falkordb_live"], False, id="expression-selecting-another-live-group-serial"
        ),
    ],
)
def test_execution_mode_follows_marker_expression_and_explicit_n(
    project: Path, args: list[str], *, expect_workers: bool
) -> None:
    output = _run_pytest(project, args)

    assert _ran_on_xdist_workers(output) is expect_workers, output


def test_live_markers_equal_the_markers_the_root_default_expression_deselects() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    default_expression = addopts[addopts.index("-m") + 1]

    deselected = set(re.findall(r"not (\w+)", default_expression))

    assert deselected == set(LIVE_MARKERS)
