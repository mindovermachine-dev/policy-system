"""Issue #55: `ps-cli` must declare a console-script entry point in its packaging metadata.

A structural, no-subprocess proof that `ps-cli/pyproject.toml` declares `ps-cli`
as a console script pointing at `ps_cli.cli:main` -- the entry point `uv tool
install`/`pip install` rely on to place `ps-cli` on `PATH` as a distributed,
installable command (AC-BI-001).
"""

import tomllib
from pathlib import Path

_PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_pyproject_declares_ps_cli_console_script_entry_point() -> None:
    """`[project.scripts] ps-cli = "ps_cli.cli:main"` is declared in ps-cli/pyproject.toml."""
    pyproject = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))

    scripts = pyproject["project"]["scripts"]

    assert scripts["ps-cli"] == "ps_cli.cli:main"
