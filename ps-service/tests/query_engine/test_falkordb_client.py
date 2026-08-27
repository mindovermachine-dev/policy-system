"""Tests for `ps_service.query_engine.falkordb_client`.

Mirrors `ps_service.company_merge.tests.test_falkordb_client`'s/
`ps_service.domain_mapper.tests.test_falkordb_client`'s shape
(PLAN_REVIEWED.md §5, Increment 2) -- this is a deliberate near-duplicate
component (own copy, not a shared import; see the module's own docstring),
so its tests are a deliberate near-duplicate too, trimmed to only what this
component actually exposes (no `check_connectivity`/graph-naming helper).
"""

from __future__ import annotations

from typing import cast

import pytest
from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

from ps_service import config as config_module
from ps_service.config import load_config
from ps_service.query_engine import falkordb_client as falkordb_client_module
from ps_service.query_engine.falkordb_client import (
    connect,
    connect_from_config,
    select_graph,
)


def test_connect_from_config_uses_env_supplied_host_and_port_not_hardcoded_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`connect_from_config` threads `ServiceConfig.falkordb_host`/
    `falkordb_port` -- resolved from `PS_FALKORDB_HOST`/`PS_FALKORDB_PORT` by
    `load_config()` -- into `connect()`, not a hardcoded default. Mocks the
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
def test_connect_and_select_graph_succeed_against_real_falkordb_instance() -> None:
    """Real connect to 127.0.0.1:6379, select the `policy_system` graph, and
    run a trivial read-only query -- requires a reachable FalkorDB instance.
    """
    db = connect(host="127.0.0.1", port=6379)
    graph = select_graph(db, "policy_system")

    graph.query("MATCH (n) RETURN n LIMIT 1")
