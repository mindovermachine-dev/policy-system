"""Fixtures shared by the ingestion tests that write through the Graph Write Gateway."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from graph_gateway._fakes import GatewayRig, close_all_rigs
from persistence.provisioned_postgres import (  # pytest discovers these by name
    fresh_provisioned,
    provisioned,
    provisioned_graph_log,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


__all__ = ["fresh_provisioned", "provisioned", "provisioned_graph_log", "rig"]


@pytest.fixture
def rig() -> Iterator[GatewayRig]:
    """A gateway over in-memory log and graph fakes; its reconciler is stopped afterwards."""
    gateway_rig = GatewayRig()
    yield gateway_rig
    close_all_rigs()
