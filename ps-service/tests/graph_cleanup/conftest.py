"""Fixtures for the Graph Cleanup tests that write through the Graph Write Gateway."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from graph_gateway._fakes import close_all_rigs

from graph_cleanup._gateway_fakes import CleanupRig
from ps_service.logging import configure

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _logging_configured() -> None:  # pyright: ignore[reportUnusedFunction]  # autouse fixture, invoked by name-collection
    """Semantic log entries are emitted by the seam; the emitter must be configured."""
    configure()


@pytest.fixture(autouse=True)
def _rigs_closed() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]  # autouse fixture, invoked by name-collection
    """Stop the reconciler of every rig a test built, including ones a helper built itself."""
    yield
    close_all_rigs()


@pytest.fixture
def cleanup_rig() -> CleanupRig:
    """A gateway over in-memory log and graph fakes with the `policy_system` graph opened.

    The autouse `_rigs_closed` fixture stops its reconciler after the test.
    """
    return CleanupRig()
