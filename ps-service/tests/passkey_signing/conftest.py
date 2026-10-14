"""Shared fixtures for ``ps_service.passkey_signing`` tests."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _process_gateway() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]  # autouse
    """Install a rig gateway as the process's for one test.

    A signed near-miss merge checks the Graph Write Gateway before it consumes the approval, as
    the app lifespan installs it in production. The import is lazy for the importlib
    cross-package reason of `mcp_interface/conftest.py`: a directory-only run cannot resolve
    `api._gateway_fakes`.
    """
    try:
        fakes = importlib.import_module("api._gateway_fakes")
    except ModuleNotFoundError:
        yield
        return
    with fakes.process_gateway_installed():
        yield
