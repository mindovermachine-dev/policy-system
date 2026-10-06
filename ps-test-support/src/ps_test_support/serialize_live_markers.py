"""Pytest plugin: keep live marker groups out of xdist (issue #197).

`addopts` in the root `pyproject.toml` runs every invocation under `pytest-xdist` (`-n auto`).
The live marker groups drive real external services -- `falkordb_live` alone spans 51 test
files that share one FalkorDB graph -- and are not safe to run concurrently. This plugin, loaded
with `-p ps_test_support.serialize_live_markers` from `addopts`, forces a single process
whenever the `-m` expression selects at least one test that carries a live marker and no `-n`
was passed on the command line.

It is a `-p` plugin rather than a `conftest.py` because pytest only loads the conftest files on
the path of the paths it was given: `pytest ps-cli/tests/...` would never load
`ps-service/tests/conftest.py`, and a live marker would silently run in parallel.

An explicit `-n` always wins -- `-n0` stays serial and `-n 2` stays parallel -- so passing a
worker count together with a live marker is an unsupported, unverified override, not a bug.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from _pytest.mark.expression import (  # pyright: ignore[reportPrivateImportUsage]
    Expression,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

LIVE_MARKERS: tuple[str, ...] = (
    "integration",
    "llm_live",
    "cellar_live",
    "falkordb_live",
    "postgres_live",
    "container_image",
)
"""The markers `pyproject.toml`'s default `-m` expression deselects (a test pins them equal)."""

_NUMPROCESSES_FLAGS = ("-n", "--numprocesses")


def _passed_n_explicitly(args: Sequence[str]) -> bool:
    """Whether the command line (not `addopts`) carries xdist's `-n`/`--numprocesses`."""
    return any(arg.startswith(_NUMPROCESSES_FLAGS) for arg in args)


def _matching_only(marker: str) -> Callable[..., bool]:
    """A `-m` matcher under which `marker` is the only marker present."""

    def matcher(name: str, /, **_kwargs: str | bool | None) -> bool:
        return name == marker

    return matcher


def _selects_live_marker(expression: str) -> bool:
    """Whether `expression` would select a test carrying any one live marker."""
    compiled = Expression.compile(expression)
    return any(compiled.evaluate(_matching_only(live)) for live in LIVE_MARKERS)


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config: pytest.Config) -> None:
    """Force a single process before xdist reads `numprocesses`, when live tests are selected."""
    expression = config.getoption("markexpr") or ""
    if _passed_n_explicitly(config.invocation_params.args) or not expression:
        return
    if _selects_live_marker(expression):
        config.option.numprocesses = 0
        config.option.dist = "no"
