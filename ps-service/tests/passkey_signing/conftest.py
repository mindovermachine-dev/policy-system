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


@pytest.fixture(autouse=True)
def _approval_executor_registry_restored() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]  # autouse
    """Put the approval-executor registry back after a test that replaced an executor in it."""
    from ps_service.passkey_signing import executors

    snapshot = dict(executors._EXECUTORS)  # pyright: ignore[reportPrivateUsage]
    try:
        yield
    finally:
        executors._EXECUTORS.clear()  # pyright: ignore[reportPrivateUsage]
        executors._EXECUTORS.update(snapshot)  # pyright: ignore[reportPrivateUsage]


@pytest.fixture(autouse=True)
def _signing_refusal_recorder_registry_restored() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]  # autouse
    """Put the signing-refusal recorder registry back after a test that replaced a recorder."""
    from ps_service.passkey_signing import executors

    snapshot = dict(executors._REFUSAL_RECORDERS)  # pyright: ignore[reportPrivateUsage]
    try:
        yield
    finally:
        executors._REFUSAL_RECORDERS.clear()  # pyright: ignore[reportPrivateUsage]
        executors._REFUSAL_RECORDERS.update(snapshot)  # pyright: ignore[reportPrivateUsage]


@pytest.fixture(autouse=True)
def _no_postgres_audit_store(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]  # autouse
    """Give `provide_audit_store` an in-memory store so no ceremony test reaches Postgres.

    A test that asserts on audit rows overrides `provide_audit_store` with its own store. The
    import is lazy for the importlib cross-package reason of `_process_gateway`.
    """
    try:
        fakes = importlib.import_module("audit._fakes")
    except ModuleNotFoundError:
        return

    def _in_memory_store(_config: object) -> object:
        return fakes.InMemoryAuditStore()

    # detroit-exception: the production store would open a real Postgres connection
    monkeypatch.setattr("ps_service.api.dependencies.PsycopgAuditStore", _in_memory_store)
