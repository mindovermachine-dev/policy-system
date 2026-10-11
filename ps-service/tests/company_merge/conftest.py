"""Fixtures for the Company Merge tests that write through the Graph Write Gateway (#210)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from graph_gateway._fakes import close_all_rigs

from company_merge._gateway_fakes import MergeRig

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def merge_rig() -> Iterator[MergeRig]:
    """A gateway over in-memory log and graph fakes with the `policy_system` graph opened."""
    rig = MergeRig()
    yield rig
    close_all_rigs()
