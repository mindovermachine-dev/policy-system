"""`ps-cli` must declare a console-script entry point in its packaging metadata.

A structural, no-subprocess proof that `ps-cli/pyproject.toml` declares `ps-cli`
as a console script pointing at `ps_cli.cli:main` -- the entry point `uv tool
install`/`pip install` rely on to place `ps-cli` on `PATH` as a distributed,
installable command.
"""

import re
import tomllib
from pathlib import Path

_PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_pyproject_declares_ps_cli_console_script_entry_point() -> None:
    """`[project.scripts] ps-cli = "ps_cli.cli:main"` is declared in ps-cli/pyproject.toml."""
    pyproject = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))

    scripts = pyproject["project"]["scripts"]

    assert scripts["ps-cli"] == "ps_cli.cli:main"
    assert set(scripts) == {"ps-cli"}


def test_pyproject_pins_truststore_exactly() -> None:
    """`truststore` is a declared dependency with an exact `==` pin."""
    pyproject = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))

    pins = [dep for dep in pyproject["project"]["dependencies"] if dep.startswith("truststore")]

    assert len(pins) == 1
    assert re.fullmatch(r"truststore==\d+(\.\d+)*", pins[0])


_PS_CLI_ROOT = Path(__file__).resolve().parent.parent
_RETIRED_MCP_PLUMBING = re.compile(
    "(?i)"
    "mcp[_-]"
    "br"
    "idge|br"
    "idge"
    "_credentials|peek_cached"
    "_access_token|self-heal|long-lived|\\bbr"
    "idge\\b"
)


def test_ps_cli_sources_and_tests_carry_no_retired_proxy_plumbing() -> None:
    """No code, test or comment in ps-cli names the retired stdio MCP proxy.

    The proxy, its credential helper, its peek-only cache accessor and the "long-lived /
    self-heal" wording are all gone; this scan keeps them from creeping back. The pattern
    is assembled from fragments so this file does not match itself.
    """
    files = [
        path
        for pattern in ("src/**/*.py", "tests/**/*.py")
        for path in _PS_CLI_ROOT.glob(pattern)
        if path != Path(__file__).resolve()
    ]

    offenders = [
        f"{path.relative_to(_PS_CLI_ROOT)}:{number}: {line.strip()}"
        for path in sorted(files)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _RETIRED_MCP_PLUMBING.search(line)
    ]

    assert offenders == []
