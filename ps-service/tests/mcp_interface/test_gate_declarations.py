"""Structural guard: every MCP tool and REST route declares how its access is controlled.

A tool registered without a `ToolGate`, or a REST route added without a role gate, an
authenticated-only marker or a public-route marker, fails this file, so a new ungated surface
cannot be added by accident. A public-route marker is only valid on a path the bearer-token
middleware leaves open, and every open path the REST router serves must carry one. The
meta-tests feed the same finders a deliberately undeclared tool and route to prove they report
them. Emission correctness per tool lives in the denial matrix; this file only checks
that a declaration exists and is well formed. It also covers REST routes (precedent:
`test_audit_parity_matrix.py`), which is why it sits beside the MCP declarations.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import APIRouter, Depends
from fastapi.routing import APIRoute
from mcp.server import MCPServer

from mcp_interface._source_modules import tool_source_trees
from mcp_interface.test_access_gate_denial_matrix import (
    _ROLE_TOOLS,  # pyright: ignore[reportPrivateUsage]  -- the matrix's own tool list is the denial-test coverage under check
)
from ps_service.api.dependencies import (
    AuthenticatedOnlyMarker,
    PublicRouteMarker,
    RoleGateDependency,
    authenticated_only,
    public_route,
)
from ps_service.api.routes import build_api_router
from ps_service.auth import middleware
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.mcp_server import TOOL_GATES
from ps_service.mcp_interface.tool_gates import GateKind
from ps_service.passkey_signing.router import build_passkey_signing_router

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ps_service.mcp_interface.tool_gates import ToolGate

_SELF_AUDITED_TOOLS = {"grant-access-role", "revoke-access-role"}
_BODY_TOOLS = {"approve-policy", "reject-policy"}
# `approve-policy` / `reject-policy` hand the policy-lifecycle service an injected enforcer; their
# error tuples keep `AccessDeniedError` because that service raises it (after the Policy read).
_INJECTED_ENFORCER_TOOLS = {"approve-policy", "reject-policy"}
_GATE_CALLS = {"check_role", "require_role"}


def find_undeclared_tools(server: MCPServer, declarations: Mapping[str, ToolGate]) -> set[str]:
    """Names registered on `server` that have no entry in `declarations`."""
    registered = {tool.name for tool in asyncio.run(server.list_tools())}
    return registered - set(declarations)


def find_undeclared_routes(router: APIRouter) -> list[str]:
    """`"<METHODS> <path>"` of every route on `router` that carries no gate declaration."""
    undeclared: list[str] = []
    for route in router.routes:
        assert isinstance(route, APIRoute)
        declared = [
            depends.dependency
            for depends in route.dependencies
            if isinstance(
                depends.dependency,
                RoleGateDependency | AuthenticatedOnlyMarker | PublicRouteMarker,
            )
        ]
        if not declared:
            undeclared.append(f"{','.join(sorted(route.methods or ()))} {route.path}")
    return undeclared


def _public_route_paths(router: APIRouter) -> set[str]:
    """Paths of the routes on `router` that carry a public-route marker."""
    paths: set[str] = set()
    for route in router.routes:
        assert isinstance(route, APIRoute)
        if any(isinstance(depends.dependency, PublicRouteMarker) for depends in route.dependencies):
            paths.add(route.path)
    return paths


def _gate_operations(router: APIRouter) -> list[tuple[str, str]]:
    """`(route "<METHOD> <path>", declared operation)` for each route carrying a role gate."""
    pairs: list[tuple[str, str]] = []
    for route in router.routes:
        assert isinstance(route, APIRoute)
        for depends in route.dependencies:
            dependency = depends.dependency
            if isinstance(dependency, RoleGateDependency):
                methods = sorted(route.methods or set())
                assert len(methods) == 1, route.path
                pairs.append((f"{methods[0]} {route.path}", dependency.gate.operation))
    return pairs


def test_every_registered_mcp_tool_has_a_gate_declaration() -> None:
    registered = {tool.name for tool in asyncio.run(mcp_server.server.list_tools())}
    assert find_undeclared_tools(mcp_server.server, TOOL_GATES) == set()
    assert set(TOOL_GATES) - registered == set()


def test_a_tool_registered_without_a_declaration_is_reported() -> None:
    throwaway = MCPServer("throwaway")

    @throwaway.tool()
    def ungated_tool() -> str:  # pyright: ignore[reportUnusedFunction]  # registered by the decorator
        return "x"

    assert find_undeclared_tools(throwaway, TOOL_GATES) == {"ungated_tool"}


def test_every_role_or_body_declaration_names_a_role_and_an_operation() -> None:
    for name, gate in TOOL_GATES.items():
        if gate.kind in {GateKind.ROLE, GateKind.BODY}:
            assert gate.role is not None, name
            assert gate.unauthenticated_message, name
            assert name, name


def test_every_authenticated_only_or_self_audited_declaration_states_a_reason() -> None:
    for name, gate in TOOL_GATES.items():
        if gate.kind in {GateKind.AUTHENTICATED_ONLY, GateKind.SELF_AUDITED}:
            assert gate.reason, name


def test_self_audited_declarations_are_exactly_grant_and_revoke() -> None:
    assert {n for n, g in TOOL_GATES.items() if g.kind is GateKind.SELF_AUDITED} == (
        _SELF_AUDITED_TOOLS
    )


def test_body_declarations_are_exactly_the_two_lifecycle_gated_tools() -> None:
    assert {n for n, g in TOOL_GATES.items() if g.kind is GateKind.BODY} == _BODY_TOOLS


def test_every_gated_mcp_tool_denies_a_caller_without_the_role() -> None:
    role_tools = {n for n, g in TOOL_GATES.items() if g.kind is GateKind.ROLE}
    assert role_tools == set(_ROLE_TOOLS)


def test_every_rest_route_declares_a_gate_or_is_an_exempt_path() -> None:
    assert find_undeclared_routes(build_api_router()) == []
    for route in build_passkey_signing_router().routes:
        assert isinstance(route, APIRoute)
        assert route.path.startswith("/approvals"), route.path


def test_every_gated_rest_route_operation_equals_method_and_path() -> None:
    pairs = _gate_operations(build_api_router())
    assert len(pairs) == 5
    for route_label, operation in pairs:
        assert operation == route_label


def test_a_route_added_without_a_declaration_is_reported() -> None:
    router = APIRouter()

    def _undeclared() -> None:
        return None

    router.add_api_route("/oops", _undeclared, methods=["GET"])
    assert find_undeclared_routes(router) == ["GET /oops"]


def test_an_authenticated_only_route_must_state_a_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        authenticated_only("")


def test_a_public_route_must_state_a_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        public_route("")


def test_every_public_route_is_on_the_bearer_middleware_open_list() -> None:
    open_paths = middleware._EXEMPT_PATHS  # pyright: ignore[reportPrivateUsage]  # the open list itself is under test
    public_paths = _public_route_paths(build_api_router())

    assert public_paths
    assert public_paths - open_paths == set()


def test_every_open_list_path_the_api_router_serves_is_declared_public() -> None:
    router = build_api_router()
    open_paths = middleware._EXEMPT_PATHS  # pyright: ignore[reportPrivateUsage]  # the open list itself is under test
    served = {route.path for route in router.routes if isinstance(route, APIRoute)}

    assert (served & open_paths) - _public_route_paths(router) == set()


def test_a_public_route_marker_declares_the_route() -> None:
    router = APIRouter()

    def _open() -> None:
        return None

    router.add_api_route(
        "/open", _open, methods=["GET"], dependencies=[Depends(public_route("open by design"))]
    )
    assert find_undeclared_routes(router) == []
    assert _public_route_paths(router) == {"/open"}


def _gated_tool_functions(directory: Path | None = None) -> dict[str, ast.FunctionDef]:
    """Map each `@_gated_tool("<name>", ...)` function in the tool modules by tool name."""
    source_file = inspect.getsourcefile(mcp_server)
    assert source_file is not None
    trees = tool_source_trees(directory or Path(source_file).parent)
    found: dict[str, ast.FunctionDef] = {}
    for tree in trees:
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Name)
                    and decorator.func.id == "_gated_tool"
                    and isinstance(decorator.args[0], ast.Constant)
                ):
                    found[str(decorator.args[0].value)] = node
    return found


def _names_holding_access_denied(directory: Path | None = None) -> set[str]:
    """Module-level tuple constants in the tool modules that include `AccessDeniedError`."""
    source_file = inspect.getsourcefile(mcp_server)
    assert source_file is not None
    trees = tool_source_trees(directory or Path(source_file).parent)
    names: set[str] = set()
    for tree in trees:
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple):
                elements = {e.id for e in node.value.elts if isinstance(e, ast.Name)}
                if "AccessDeniedError" in elements:
                    names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def test_the_ast_walk_finds_every_role_gated_tool() -> None:
    role_tools = {n for n, g in TOOL_GATES.items() if g.kind is GateKind.ROLE}
    assert role_tools <= set(_gated_tool_functions())


def test_no_gated_tool_body_catches_access_denied_error() -> None:
    holders = _names_holding_access_denied() | {"AccessDeniedError"}
    offenders: list[str] = []
    for name, function in _gated_tool_functions().items():
        gate = TOOL_GATES[name]
        if gate.kind not in {GateKind.ROLE, GateKind.BODY} or name in _INJECTED_ENFORCER_TOOLS:
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.ExceptHandler) and node.type is not None:
                referenced = {n.id for n in ast.walk(node.type) if isinstance(n, ast.Name)}
                if referenced & holders:
                    offenders.append(f"{name}: except {sorted(referenced & holders)}")
            if isinstance(node, ast.Call):
                callee = node.func
                called = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", "")
                if called in _GATE_CALLS:
                    offenders.append(f"{name}: calls {called}")
    assert offenders == []


def test_the_ast_walks_see_a_probe_in_any_tool_group_module(tmp_path: Path) -> None:
    probe = tmp_path / "probe_tools.py"
    probe.write_text(
        "_DENIED = (AccessDeniedError, ValueError)\n"
        '@_gated_tool("probe-tool")\n'
        "def probe_tool() -> str:\n"
        '    return "x"\n',
        encoding="utf-8",
    )

    assert "probe-tool" in _gated_tool_functions(tmp_path)
    assert "_DENIED" in _names_holding_access_denied(tmp_path)
    assert "probe-tool" not in _gated_tool_functions()


def test_every_gated_tool_name_is_defined_exactly_once() -> None:
    names = [
        decorator.args[0].value
        for tree in tool_source_trees()
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "_gated_tool"
        and isinstance(decorator.args[0], ast.Constant)
    ]

    assert len(names) == len(set(names))
