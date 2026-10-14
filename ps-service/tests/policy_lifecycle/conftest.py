"""Fixtures for the Policy Lifecycle tests that write through the Graph Write Gateway."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from graph_gateway._fakes import close_all_rigs

from policy_lifecycle._gateway_fakes import LifecycleRig

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def lifecycle_rig() -> Iterator[LifecycleRig]:
    """A gateway over in-memory log and graph fakes with the `policy_system` graph opened."""
    rig = LifecycleRig()
    yield rig
    close_all_rigs()
