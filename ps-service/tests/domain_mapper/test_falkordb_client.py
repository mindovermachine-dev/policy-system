"""Tests for `ps_service.domain_mapper.falkordb_client`.

Mirrors `ps_service.ingestion.tests.test_falkordb_client`'s shape exactly
(PLAN_REVIEWED.md §11 Increment 4) — this is a deliberate near-duplicate
component (own copy, not a shared import; see the module's own docstring),
so its tests are a deliberate near-duplicate too.
"""

from __future__ import annotations

from typing import cast

import pytest
from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

from ps_service import config as config_module
from ps_service.config import load_config
from ps_service.dependency_health import FALKORDB, is_healthy
from ps_service.domain_mapper import falkordb_client as falkordb_client_module
from ps_service.domain_mapper.errors import DomainMapperConfigurationError
from ps_service.domain_mapper.falkordb_client import (
    baseline_graph_name,
    check_connectivity,
    connect,
    connect_from_config,
    native_graph_name,
)


class _FakeConnectivityProbeThatRaises:
    """Satisfies `_ConnectivityProbe` structurally; `list_graphs` always
    fails, simulating an unreachable FalkorDB instance."""

    def list_graphs(self) -> list[str]:
        raise ConnectionRefusedError("connection refused")


class _FakeConnectivityProbeThatSucceeds:
    """Satisfies `_ConnectivityProbe` structurally; `list_graphs` returns
    normally, simulating a reachable FalkorDB instance."""

    def list_graphs(self) -> list[str]:
        return ["cra_baseline"]


def test_check_connectivity_raises_domain_mapper_configuration_error_on_failure() -> None:
    probe = _FakeConnectivityProbeThatRaises()

    with pytest.raises(DomainMapperConfigurationError) as exc_info:
        check_connectivity(probe, host="127.0.0.1", port=6379)

    assert "127.0.0.1:6379" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ConnectionRefusedError)


def test_check_connectivity_does_not_raise_when_list_graphs_succeeds() -> None:
    probe = _FakeConnectivityProbeThatSucceeds()

    check_connectivity(probe, host="127.0.0.1", port=6379)


def test_check_connectivity_marks_falkordb_unhealthy_on_failure() -> None:
    probe = _FakeConnectivityProbeThatRaises()

    with pytest.raises(DomainMapperConfigurationError):
        check_connectivity(probe, host="127.0.0.1", port=6379)

    assert is_healthy(FALKORDB) is False


def test_check_connectivity_marks_falkordb_healthy_on_success() -> None:
    probe = _FakeConnectivityProbeThatSucceeds()

    check_connectivity(probe, host="127.0.0.1", port=6379)

    assert is_healthy(FALKORDB) is True


@pytest.mark.parametrize(
    ("short_name", "expected_graph_name"),
    [
        ("CRA", "cra_native"),
        ("GDPR", "gdpr_native"),
        ("NIS2", "nis2_native"),
    ],
)
def test_native_graph_name_lowercases_and_appends_native_suffix(
    short_name: str, expected_graph_name: str
) -> None:
    assert native_graph_name(short_name) == expected_graph_name


@pytest.mark.parametrize(
    ("short_name", "expected_graph_name"),
    [
        ("CRA", "cra_baseline"),
        ("GDPR", "gdpr_baseline"),
        ("NIS2", "nis2_baseline"),
    ],
)
def test_baseline_graph_name_lowercases_and_appends_baseline_suffix(
    short_name: str, expected_graph_name: str
) -> None:
    assert baseline_graph_name(short_name) == expected_graph_name


def test_connect_from_config_uses_env_supplied_host_and_port_not_hardcoded_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`connect_from_config` threads `ServiceConfig.falkordb_host`/
    `falkordb_port` — resolved from `PS_FALKORDB_HOST`/`PS_FALKORDB_PORT` by
    `load_config()` — into `connect()`, not a hardcoded default. Mocks the
    module-level `connect` (the underlying call) to capture exactly what it
    was invoked with, so this fails if `connect_from_config` ever hardcodes
    a literal instead of reading `config`.
    """
    env_host = "10.20.30.40"
    env_port = 7000
    assert env_host != config_module._DEFAULT_FALKORDB_HOST
    assert env_port != config_module._DEFAULT_FALKORDB_PORT
    monkeypatch.setenv("PS_FALKORDB_HOST", env_host)
    monkeypatch.setenv("PS_FALKORDB_PORT", str(env_port))
    captured: dict[str, object] = {}
    sentinel = cast(FalkorDB, object())

    def _fake_connect(host: str, port: int) -> FalkorDB:
        captured["host"] = host
        captured["port"] = port
        return sentinel

    monkeypatch.setattr(falkordb_client_module, "connect", _fake_connect)

    config = load_config()
    result = connect_from_config(config)

    assert captured == {"host": env_host, "port": env_port}
    assert result is sentinel


@pytest.mark.falkordb_live
def test_check_connectivity_succeeds_against_real_falkordb_instance() -> None:
    """Real connect to 127.0.0.1:6379 — requires a reachable FalkorDB
    instance."""
    db = connect(host="127.0.0.1", port=6379)

    check_connectivity(db, host="127.0.0.1", port=6379)
