"""Shared fixtures for ``ps_service.mcp_interface`` tests."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _default_cellar_stub(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stub the Cellar fetch boundary for every non-live test (issue #193, CHANGES.md H1).

    Every ingestion request now resolves via Cellar, so a test that reaches the
    ``ingest_regulation`` tool with a fake pipeline must never hit the real
    service. Skipped for tests carrying a live marker; a per-test patch overrides it.

    ``tests/api`` is an importable package only once pytest has collected it, so a
    run of this directory alone (without ``tests/api``) cannot resolve ``api._fakes``.
    Every test that needs the stub imports ``api._fakes`` itself and cannot be
    collected in that situation either, so a missing package is skipped here rather
    than breaking the unrelated tests of a directory-only run.
    """
    try:
        fakes = importlib.import_module("api._fakes")
    except ModuleNotFoundError:
        return
    fakes.install_default_cellar_stub_unless_live(request, monkeypatch)
