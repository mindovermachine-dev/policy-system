"""Fail-closed `GateEnvironment` builders for MCP tool tests.

Every store and connection factory of the environment built here raises
`UnoverriddenFactoryError` unless the test overrides it, so a tool that reaches an unexpected
factory stops instead of opening a real connection. The tool boundary turns an exception into a
generic `error:` result, so each refused factory name is also recorded in `touched`, which a
fixture can assert is empty after the call.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, NoReturn, Protocol, cast

from ps_service.config import load_config
from ps_service.mcp_interface.mcp_server import build_server
from ps_service.mcp_interface.tool_core import resolve_authz_actor, run_mcp_action
from ps_service.mcp_interface.tool_gates import GateEnvironment
from ps_service.query_engine.falkordb_client import select_graph

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from ps_service.config import ServiceConfig
    from ps_service.graph_gateway.read_only import ReadOnlyDatabase
    from ps_service.query_engine.falkordb_client import GraphHandle

FACTORY_FIELDS = (
    "audit_store_factory",
    "role_store_factory",
    "pending_approval_store_factory",
    "ingestion_run_store_factory",
    "runtime_config_store_factory",
    "graph_factory",
)
"""The environment fields that build a store or open a connection."""

touched: list[str] = []
"""Names of the factories a fail-closed environment refused, in call order."""


class UnoverriddenFactoryError(Exception):
    """A tool reached a factory the test did not override."""


def _refuse(field_name: str) -> Callable[..., NoReturn]:
    def _factory(*_args: object, **_kwargs: object) -> NoReturn:
        touched.append(field_name)
        message = f"{field_name} was not overridden by the test"
        raise UnoverriddenFactoryError(message)

    return _factory


def fail_closed_environment(**overrides: object) -> GateEnvironment:
    """Build an environment whose store and connection factories raise unless overridden.

    `load_config`, `resolve_actor` and `run_action` stay real: they open no connection.

    Args:
        **overrides: Replacement values for any `GateEnvironment` field.

    Returns:
        The environment.
    """
    base = GateEnvironment(
        load_config=load_config,
        resolve_actor=resolve_authz_actor,
        run_action=run_mcp_action,
        audit_store_factory=_refuse("audit_store_factory"),
        role_store_factory=_refuse("role_store_factory"),
        pending_approval_store_factory=_refuse("pending_approval_store_factory"),
        ingestion_run_store_factory=_refuse("ingestion_run_store_factory"),
        runtime_config_store_factory=_refuse("runtime_config_store_factory"),
        graph_factory=_refuse("graph_factory"),
    )
    return dataclasses.replace(base, **overrides)


class SelectsGraph(Protocol):
    """A database double: anything that hands out a graph handle by name."""

    def select_graph(self, name: str, /) -> object:
        """Return the handle for graph `name`."""
        ...


def graph_factory_for(database: SelectsGraph) -> Callable[[ServiceConfig], GraphHandle]:
    """Return a `graph_factory` that selects the configured graph on `database`."""

    def _factory(config: ServiceConfig) -> GraphHandle:
        return select_graph(cast("ReadOnlyDatabase", database), config.falkordb_graph)

    return _factory


def build_test_server(**overrides: object) -> MCPServer:
    """Build a server over `fail_closed_environment(**overrides)` and return it."""
    return build_server(fail_closed_environment(**overrides)).server


def graph_factory_from_connect(
    connect: Callable[[ServiceConfig], SelectsGraph],
) -> Callable[[ServiceConfig], GraphHandle]:
    """Return a `graph_factory` that selects the configured graph on `connect(config)`."""

    def _factory(config: ServiceConfig) -> GraphHandle:
        return graph_factory_for(connect(config))(config)

    return _factory
