"""Shared fixtures for ``ps_service.change_monitor`` tests."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _process_gateway() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]  # autouse
    """Install a rig gateway as the process's for one test.

    The amendment re-ingest obtains its Graph Write Gateway from the process, as the app
    lifespan installs it in production. The import is lazy: `tests/api` is an importable package
    only once pytest has collected it, and the tests that need a gateway cannot be collected
    without it either.
    """
    try:
        fakes = importlib.import_module("api._gateway_fakes")
    except ModuleNotFoundError:
        yield
        return
    with fakes.process_gateway_installed():
        yield
