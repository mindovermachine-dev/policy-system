"""Fixtures for the `postgres_live` tests, re-exported from `persistence.provisioned_postgres`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persistence.provisioned_postgres import (  # pytest discovers these by name
    fresh_provisioned,
    provisioned,
    provisioned_graph_log,
)

from graph_gateway._fakes import close_all_rigs

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["close_gateway_rigs", "fresh_provisioned", "provisioned", "provisioned_graph_log"]


@pytest.fixture(autouse=True)
def close_gateway_rigs() -> Iterator[None]:
    """Stop the reconciler threads of every gateway rig a test built."""
    yield
    close_all_rigs()
