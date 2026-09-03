"""Shared pytest fixtures for `ps-cli/tests/`.

See CHANGES.md (issue #56) F2: closes FLAWS.md's blocking finding that, once
`load_config()`'s `config_dir` default becomes `resolve_config_dir()` (real
`$HOME/.config/ps-cli/` absent `PS_CLI_CONFIG_DIR`), every pre-existing
`test_config.py` test calling bare `load_config()` would silently start
reading real machine state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_ps_cli_config_dir(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every ps-cli test gets a fresh, empty config dir by default so no test ever reads
    or writes the real machine's $HOME/.config/ps-cli/ (PLAN.md D2's default). Individual
    tests may still override PS_CLI_CONFIG_DIR or pass config_dir=... explicitly.
    """
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(tmp_path / "isolated-ps-cli-config"))
