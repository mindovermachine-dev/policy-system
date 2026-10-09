"""Tests for the graph log store when `ps_state` is unreachable or unconfigured (AC-BI-009).

Hermetic: the unreachable case connects to a local port nothing listens on, so the real driver
failure is exercised without a Postgres server.
"""

from __future__ import annotations

import dataclasses
import socket
from typing import TYPE_CHECKING

import psycopg
import pytest

from ps_service.config import load_config
from ps_service.dependency_health import STATE_POSTGRES, is_healthy, mark_healthy
from ps_service.graph_gateway.errors import GRAPH_LOG_UNAVAILABLE_MESSAGE, GraphLogUnavailableError
from ps_service.graph_gateway.models import GraphLogEntryDraft, GraphLogGroupDraft
from ps_service.graph_gateway.store import PsycopgGraphLogStore
from ps_service.persistence import StatePostgresConnectionError

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig

_USER = "ps_state_secret_user"
_PASSWORD = "very-secret-password"  # fixture value, asserted absent from messages
_HOST = "127.0.0.1"


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind((_HOST, 0))
        return int(probe.getsockname()[1])


def _unreachable_config() -> ServiceConfig:
    return dataclasses.replace(
        load_config(),
        state_postgres_host=_HOST,
        state_postgres_port=_unused_port(),
        state_postgres_database="ps_state",
        state_postgres_user=_USER,
        state_postgres_password=_PASSWORD,
    )


def _group() -> GraphLogGroupDraft:
    entry = GraphLogEntryDraft(name="Capability", identity="cap-1", content={})
    return GraphLogGroupDraft(graph="compliance", entries=(entry,))


def test_unreachable_ps_state_raises_sanitized_unavailable_error() -> None:
    config = _unreachable_config()
    mark_healthy(STATE_POSTGRES)

    with pytest.raises(GraphLogUnavailableError) as raised:
        PsycopgGraphLogStore(config).append_group_standalone(_group())

    exc = raised.value
    assert isinstance(exc, StatePostgresConnectionError)
    assert str(exc) == GRAPH_LOG_UNAVAILABLE_MESSAGE
    for secret in (_HOST, str(config.state_postgres_port), _USER, _PASSWORD, "password"):
        assert secret not in str(exc)
    assert isinstance(exc.__cause__, psycopg.Error)
    assert not is_healthy(STATE_POSTGRES)


def test_unreachable_ps_state_raises_the_same_error_on_read() -> None:
    store = PsycopgGraphLogStore(_unreachable_config())

    with pytest.raises(GraphLogUnavailableError):
        store.read_entries("compliance")
    with pytest.raises(GraphLogUnavailableError):
        store.last_position("compliance")


def test_unconfigured_ps_state_raises_sanitized_unavailable_error() -> None:
    config = dataclasses.replace(load_config(), state_postgres_host=None)

    with pytest.raises(GraphLogUnavailableError) as raised:
        PsycopgGraphLogStore(config).append_group_standalone(_group())

    assert str(raised.value) == GRAPH_LOG_UNAVAILABLE_MESSAGE
    assert isinstance(raised.value.__cause__, StatePostgresConnectionError)
    assert not is_healthy(STATE_POSTGRES)


def test_unreachable_ps_state_raises_the_same_error_for_marker_and_checkpoint_operations() -> None:
    store = PsycopgGraphLogStore(_unreachable_config())

    with pytest.raises(GraphLogUnavailableError):
        store.read_applied_position("compliance")
    with pytest.raises(GraphLogUnavailableError):
        store.advance_applied_position("compliance", 1)
    with pytest.raises(GraphLogUnavailableError):
        store.record_digest_checkpoint("compliance", 1, "digest")
    with pytest.raises(GraphLogUnavailableError):
        store.read_digest_checkpoint("compliance", 1)
