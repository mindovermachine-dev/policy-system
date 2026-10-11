"""Fixtures shared by the Domain Mapper tests that write through the Graph Write Gateway (#209)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from graph_gateway._fakes import close_all_rigs

from domain_mapper._gateway_fakes import BaselineRig

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def baseline_rig() -> Iterator[BaselineRig]:
    """A gateway over in-memory log and graph fakes with a baseline read handle."""
    rig = BaselineRig()
    yield rig
    close_all_rigs()


@pytest.fixture
def other_baseline_rig() -> Iterator[BaselineRig]:
    """A second, independent rig, for tests that prove two baseline graphs never mix."""
    rig = BaselineRig()
    yield rig
    close_all_rigs()
