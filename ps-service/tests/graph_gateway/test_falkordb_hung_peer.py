"""A FalkorDB that accepts the connection but never answers fails within the configured bound.

The peer is a real local socket that goes silent, so the socket timeout from `ServiceConfig`
reaches the driver for real; the gateway turns the resulting `TimeoutError` into
`GraphUnavailableError` and marks FalkorDB unhealthy.
"""

from __future__ import annotations

import time

import pytest
import redis.exceptions

from graph_gateway._fakes import InMemoryGraphLogStore
from graph_gateway.silent_peer import silent_peer
from ps_service.config import ServiceConfig
from ps_service.dependency_health import FALKORDB, is_healthy
from ps_service.graph_gateway.connection import connect_from_config
from ps_service.graph_gateway.default_gateway import FalkorDBGraphOpener
from ps_service.graph_gateway.errors import GraphUnavailableError
from ps_service.graph_gateway.gateway import GatewaySettings, GraphWriteGateway
from ps_service.graph_gateway.models import MutationGroup, UpsertNode

_SOCKET_TIMEOUT_SECONDS = 1.0
_TOLERANCE_SECONDS = 1.5
_ATTEMPTS = 2


def _config(port: int) -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        falkordb_host="127.0.0.1",
        falkordb_port=port,
        falkordb_connect_timeout_seconds=_SOCKET_TIMEOUT_SECONDS,
        falkordb_socket_timeout_seconds=_SOCKET_TIMEOUT_SECONDS,
        query_timeout_ms=100,
    )


def _group() -> MutationGroup:
    return MutationGroup(
        graph="compliance",
        audit_event_id="3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10",
        primitives=(UpsertNode(label="Capability", id="cap-1", properties={"name": "a"}),),
    )


def test_a_peer_that_never_replies_raises_timeout_error_within_the_socket_timeout() -> None:
    with silent_peer() as peer:
        started = time.monotonic()
        with pytest.raises(redis.exceptions.TimeoutError):
            connect_from_config(_config(peer.port))
        elapsed = time.monotonic() - started

    assert elapsed < _SOCKET_TIMEOUT_SECONDS + _TOLERANCE_SECONDS


def test_a_command_on_an_established_connection_times_out_once_without_driver_retries() -> None:
    with silent_peer(completes_driver_handshake=True) as peer:
        database = connect_from_config(_config(peer.port))

        started = time.monotonic()
        with pytest.raises(redis.exceptions.TimeoutError):
            database.list_graphs()
        elapsed = time.monotonic() - started

        assert peer.accepted_connections == 1
    assert elapsed < _SOCKET_TIMEOUT_SECONDS + _TOLERANCE_SECONDS


def test_a_hung_falkordb_fails_the_gateway_call_fast_and_marks_falkordb_unhealthy() -> None:
    with silent_peer(completes_driver_handshake=True) as peer:
        gateway = GraphWriteGateway(
            log_store=InMemoryGraphLogStore(events=[]),
            graph_opener=FalkorDBGraphOpener(_config(peer.port)),
            settings=GatewaySettings(max_attempts=_ATTEMPTS, initial_backoff_seconds=0),
            sleep=lambda _seconds: None,
            reconciler_wait=lambda _delay: True,
        )

        started = time.monotonic()
        with pytest.raises(GraphUnavailableError) as raised:
            gateway.submit_group(_group())
        elapsed = time.monotonic() - started

    assert isinstance(raised.value.__cause__, redis.exceptions.TimeoutError)
    assert is_healthy(FALKORDB) is False
    assert elapsed < _ATTEMPTS * (_SOCKET_TIMEOUT_SECONDS + _TOLERANCE_SECONDS)
