"""The production composition of the Graph Write Gateway (issue #206, S13b).

`build_default_graph_write_gateway(config)` wires the real log store and a FalkorDB opener from
the service config without touching either service: building must never fail because Postgres
or FalkorDB is unreachable, since that is decided later, per call, with the fail-closed errors.
"""

from __future__ import annotations

import pytest
import redis.exceptions

from ps_service.config import ServiceConfig
from ps_service.graph_gateway.default_gateway import (
    FalkorDBGraphOpener,
    build_default_graph_write_gateway,
)
from ps_service.graph_gateway.errors import GraphLogUnavailableError
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.models import GraphLeg, MultiGraphGroup, UpsertNode

_UNREACHABLE_PORT = 1


def _config(**overrides: object) -> ServiceConfig:
    defaults: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8000,
        "graceful_shutdown_seconds": 10,
        "logging_dir": None,
    }
    defaults.update(overrides)
    return ServiceConfig(**defaults)  # pyright: ignore[reportArgumentType]  # dict-unpacked kwargs


def test_building_the_default_gateway_connects_to_nothing_and_starts_no_thread() -> None:
    gateway = build_default_graph_write_gateway(_config(falkordb_port=_UNREACHABLE_PORT))

    assert isinstance(gateway, GraphWriteGateway)
    assert gateway.is_reconciling is False


def test_default_gateway_without_a_state_postgres_fails_closed_on_recovery() -> None:
    gateway = build_default_graph_write_gateway(_config())

    with pytest.raises(GraphLogUnavailableError):
        gateway.recover()


def test_graph_opener_raises_the_transient_connection_error_and_retries_the_connection() -> None:
    opener = FalkorDBGraphOpener(_config(falkordb_port=_UNREACHABLE_PORT))

    for _ in range(2):  # a failed connect is not cached
        with pytest.raises(redis.exceptions.ConnectionError):
            opener("compliance")


def test_the_default_gateway_has_a_key_port_so_a_swap_group_is_not_refused_as_unconfigured() -> (
    None
):
    gateway = build_default_graph_write_gateway(_config())
    group = MultiGraphGroup(
        audit_event_id="3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10",
        legs=(
            GraphLeg(graph="native", primitives=(UpsertNode(label="Capability", id="n-1"),)),
            GraphLeg(graph="baseline", primitives=(UpsertNode(label="Capability", id="b-1"),)),
        ),
        apply="swap",
    )

    with pytest.raises(GraphLogUnavailableError):
        gateway.submit_multi_graph_group(group)
