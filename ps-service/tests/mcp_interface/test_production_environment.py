"""Tests for `ps_service.mcp_interface.production_environment`.

The production environment wires every factory of a `GateEnvironment` to the real store class or
connection opener. Each test builds it and calls one factory with no override, so a factory that
is wired to the wrong class, or that bypasses the audit store it should build on, fails here.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, cast

from ps_service.audit.store import PsycopgAuditStore
from ps_service.authz.store import PsycopgAccessRoleStore
from ps_service.config import load_config
from ps_service.ingestion_runs import PsycopgIngestionRunStore
from ps_service.logging import configure
from ps_service.mcp_interface.production_environment import build_production_environment
from ps_service.mcp_interface.tool_core import resolve_authz_actor, run_mcp_action
from ps_service.mcp_interface.tool_gates import GateEnvironment
from ps_service.passkey_signing.store import PsycopgPendingApprovalStore
from ps_service.query_engine import falkordb_client
from ps_service.runtime_config import PsycopgRuntimeConfigStore

if TYPE_CHECKING:
    import pytest

    from ps_service.audit.store import AuditStore
    from ps_service.config import ServiceConfig


def test_the_production_environment_is_a_gate_environment() -> None:
    assert isinstance(build_production_environment(), GateEnvironment)


def test_the_production_environment_loads_the_service_configuration() -> None:
    assert build_production_environment().load_config() == load_config()


def test_the_production_environment_resolves_the_actor_from_the_verified_token() -> None:
    environment = build_production_environment()

    assert environment.resolve_actor(load_config()) == resolve_authz_actor(load_config())
    assert environment.resolve_actor(load_config()) is None


def test_the_production_environment_runs_a_tool_body_inside_the_action_log_pair() -> None:
    configure()
    environment = build_production_environment()

    assert environment.run_action("probe", lambda: "ok") == run_mcp_action("probe", lambda: "ok")


def test_the_production_audit_factory_builds_the_postgres_audit_store() -> None:
    assert isinstance(
        build_production_environment().audit_store_factory(load_config()), PsycopgAuditStore
    )


def test_the_production_role_factory_builds_the_postgres_role_store_over_the_given_audit() -> None:
    audit_store = cast("AuditStore", object())

    store = build_production_environment().role_store_factory(
        load_config(), audit_store=audit_store
    )

    assert isinstance(store, PsycopgAccessRoleStore)
    assert store._audit_store is audit_store  # pyright: ignore[reportPrivateUsage]  # the composed audit store is only observable on the built store


def test_the_production_role_store_holds_the_audit_store_the_audit_factory_built() -> None:
    sentinel = cast("AuditStore", object())
    built: list[AuditStore] = []

    def _audit_factory(_config: ServiceConfig) -> AuditStore:
        built.append(sentinel)
        return sentinel

    environment = dataclasses.replace(
        build_production_environment(),
        audit_store_factory=cast("object", _audit_factory),  # pyright: ignore[reportArgumentType]  # a sentinel stands in for the store the factory builds
    )

    store = environment.access_role_store(load_config())

    assert isinstance(store, PsycopgAccessRoleStore)
    assert store._audit_store is sentinel  # pyright: ignore[reportPrivateUsage]  # the composed audit store is only observable on the built store
    assert built == [sentinel]


def test_the_default_role_store_is_built_over_a_postgres_audit_store() -> None:
    store = build_production_environment().access_role_store(load_config())

    assert isinstance(store, PsycopgAccessRoleStore)
    assert isinstance(store._audit_store, PsycopgAuditStore)  # pyright: ignore[reportPrivateUsage]  # the composed audit store is only observable on the built store


def test_the_production_pending_approval_factory_builds_the_postgres_store() -> None:
    assert isinstance(
        build_production_environment().pending_approval_store_factory(load_config()),
        PsycopgPendingApprovalStore,
    )


def test_the_production_ingestion_run_factory_builds_the_postgres_store_over_the_audit_store() -> (
    None
):
    store = build_production_environment().ingestion_run_store(load_config())

    assert isinstance(store, PsycopgIngestionRunStore)
    assert isinstance(store._audit_store, PsycopgAuditStore)  # pyright: ignore[reportPrivateUsage]  # the composed audit store is only observable on the built store


def test_the_production_runtime_config_factory_builds_the_postgres_store_over_the_audit_store() -> (
    None
):
    store = build_production_environment().runtime_config_store(load_config())

    assert isinstance(store, PsycopgRuntimeConfigStore)
    assert isinstance(store._audit_store, PsycopgAuditStore)  # pyright: ignore[reportPrivateUsage]  # the composed audit store is only observable on the built store


def test_the_production_graph_factory_selects_the_configured_graph_on_the_read_only_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected: list[tuple[str, int]] = []
    selected: list[str] = []
    handle = object()

    class _Database:
        def select_graph(self, name: str) -> object:
            selected.append(name)
            return handle

    def _connect(*, host: str, port: int) -> _Database:
        connected.append((host, port))
        return _Database()

    monkeypatch.setattr(falkordb_client, "connect", _connect)
    config = load_config()

    graph = build_production_environment().graph_factory(config)

    assert connected == [(config.falkordb_host, config.falkordb_port)]
    assert selected == [config.falkordb_graph]
    assert graph is not None
