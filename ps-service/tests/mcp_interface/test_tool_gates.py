"""Tests for the `GateEnvironment` fields, their composition and per-server gate registries.

The environment carries every store and connection factory the MCP tools use; this module covers
the fail-closed test environment, the composition of dependent factories and the registration of
gates on more than one server in a process.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from typing import TYPE_CHECKING, Annotated, cast

import pytest
from audit._fakes import InMemoryAuditStore, audit_store_factory
from authz._fakes import FakeAccessRoleStore
from curated_source._fakes import InMemoryRuntimeConfigStore
from ingestion_runs._fakes import InMemoryIngestionRunStore
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from mcp_interface._environment import (
    FACTORY_FIELDS,
    UnoverriddenFactoryError,
    build_test_server,
    fail_closed_environment,
    touched,
)
from mcp_interface.test_access_role_tools import (
    _ACTOR_ISSUER as _ISSUER,  # pyright: ignore[reportPrivateUsage]  # reuse the sibling test module's fixtures verbatim
)
from mcp_interface.test_access_role_tools import (
    _verified_actor,  # pyright: ignore[reportPrivateUsage]  # reuse the sibling test module's fixtures verbatim
)
from ps_service.api.errors import AccessDeniedError
from ps_service.authz.models import AccessRole
from ps_service.authz.store import PsycopgAccessRoleStore
from ps_service.config import load_config
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.tool_core import (
    ACCESS_ROLE_CHANGE_GATE,
    ACCESS_ROLE_MANAGEMENT_GATE,
    CLEANUP_GATE,
    COMPLIANCE_OFFICER_GATE,
    NEAR_MISS_KEEP_SEPARATE_GATE,
    OWN_APPROVAL_READ_GATE,
    PLAIN_READ_GATE,
    POLICY_AUTHOR_GATE,
    POLICY_DRAFT_ACCESS_GATE,
    SYSTEM_ADMIN_GATE,
    BuiltServer,
    cleanup_dependencies,
    create_server,
    declared_tool,
)
from ps_service.mcp_interface.tool_gates import (
    BypassPolicy,
    GateEnvironment,
    GateKind,
    ToolGate,
    bind_environment,
    gated_tool,
    lifecycle_role_enforcer,
    register_declared_tools,
)
from ps_service.query_engine import falkordb_client

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.audit.store import AuditStore
    from ps_service.config import ServiceConfig

_COMPOSED = (
    ("access_role_store", "role_store_factory"),
    ("ingestion_run_store", "ingestion_run_store_factory"),
    ("runtime_config_store", "runtime_config_store_factory"),
)


def _audit_factory_returning(store: AuditStore) -> Callable[[ServiceConfig], AuditStore]:
    def _factory(_config: ServiceConfig) -> AuditStore:
        return store

    return _factory


def test_the_fail_closed_environment_covers_every_factory_field() -> None:
    environment_fields = {f.name for f in dataclasses.fields(fail_closed_environment())}

    assert set(FACTORY_FIELDS) <= environment_fields
    assert {name for name in environment_fields if name.endswith(("_factory",))} <= set(
        FACTORY_FIELDS
    )


@pytest.mark.parametrize("field_name", FACTORY_FIELDS)
def test_every_unoverridden_factory_raises_and_is_recorded(field_name: str) -> None:
    touched.clear()
    environment = fail_closed_environment()
    factory = cast("Callable[..., object]", getattr(environment, field_name))

    with pytest.raises(UnoverriddenFactoryError, match=field_name):
        factory(load_config(), audit_store=cast("AuditStore", object()))

    assert touched == [field_name]


def test_an_overridden_factory_is_used_and_the_others_stay_closed() -> None:
    touched.clear()
    sentinel = cast("AuditStore", object())
    environment = fail_closed_environment(audit_store_factory=_audit_factory_returning(sentinel))

    assert environment.audit_store_factory(load_config()) is sentinel
    with pytest.raises(UnoverriddenFactoryError):
        environment.graph_factory(load_config())


def test_an_audit_override_reaches_the_role_store_of_the_fail_closed_environment() -> None:
    received: list[AuditStore] = []
    sentinel = cast("AuditStore", object())

    def _role_factory(config: ServiceConfig, /, *, audit_store: AuditStore) -> object:
        received.append(audit_store)
        return PsycopgAccessRoleStore(config, audit_store=audit_store)

    environment = fail_closed_environment(
        audit_store_factory=_audit_factory_returning(sentinel),
        role_store_factory=_role_factory,
    )

    environment.access_role_store(load_config())

    assert received == [sentinel]


@pytest.mark.parametrize(("method", "factory_field"), _COMPOSED)
def test_composed_store_methods_hand_the_audit_factory_result_to_their_factory(
    method: str, factory_field: str
) -> None:
    sentinel_audit = cast("AuditStore", object())
    sentinel_store = object()
    received: list[AuditStore] = []

    def _factory(_config: ServiceConfig, /, *, audit_store: AuditStore) -> object:
        received.append(audit_store)
        return sentinel_store

    environment = fail_closed_environment(
        audit_store_factory=_audit_factory_returning(sentinel_audit), **{factory_field: _factory}
    )

    built = cast("Callable[[ServiceConfig], object]", getattr(environment, method))(load_config())

    assert built is sentinel_store
    assert received == [sentinel_audit]


_PLAIN_GATE = ToolGate(kind=GateKind.AUTHENTICATED_ONLY, reason="a throwaway tool for the test")
_ROLE_GATE = ToolGate(
    kind=GateKind.ROLE,
    role=AccessRole.SYSTEM_ADMIN,
    bypass=BypassPolicy.SKIP,
    unauthenticated_message="error: no caller",
)
_NON_LIFECYCLE_BODY_GATE = ToolGate(
    kind=GateKind.BODY,
    role=AccessRole.COMPLIANCE_OFFICER,
    bypass=BypassPolicy.REFUSE,
    unauthenticated_message="error: no caller",
)


def _throwaway_tool() -> str:
    return "ok"


def test_gates_registered_with_their_own_registry_do_not_touch_the_production_registry() -> None:
    gates_a: dict[str, ToolGate] = {}
    gates_b: dict[str, ToolGate] = {}
    environment = fail_closed_environment()

    for server, gates in ((MCPServer(name="a"), gates_a), (MCPServer(name="b"), gates_b)):
        register = gated_tool(server, environment, gates)

        register("throwaway-tool", _PLAIN_GATE)(_throwaway_tool)

    assert gates_a == {"throwaway-tool": _PLAIN_GATE}
    assert gates_b == {"throwaway-tool": _PLAIN_GATE}
    assert "throwaway-tool" not in mcp_server.TOOL_GATES


def test_a_duplicate_tool_name_in_one_registry_is_refused() -> None:
    gates: dict[str, ToolGate] = {}
    register = gated_tool(MCPServer(name="a"), fail_closed_environment(), gates)

    register("throwaway-tool", _PLAIN_GATE)(_throwaway_tool)

    with pytest.raises(ValueError, match="already has a gate declaration"):
        register("throwaway-tool", _ROLE_GATE)(_throwaway_tool)


_NO_CALLER = "error: no caller"
_REFUSING_GATE = ToolGate(
    kind=GateKind.ROLE,
    role=AccessRole.SYSTEM_ADMIN,
    bypass=BypassPolicy.REFUSE,
    unauthenticated_message=_NO_CALLER,
)
_received: list[GateEnvironment] = []


@declared_tool("echo-field", _PLAIN_GATE)
def echo_field(
    environment: GateEnvironment, text: Annotated[str, Field(min_length=3, description="the text")]
) -> str:
    _received.append(environment)
    return text


@declared_tool("echo-context", _PLAIN_GATE)
def echo_context(environment: GateEnvironment, ctx: Context, text: str) -> str:
    _received.append(environment)
    return f"{text}:{type(ctx).__name__}"


@declared_tool("echo-async", _PLAIN_GATE)
async def echo_async(environment: GateEnvironment, text: str) -> str:
    _received.append(environment)
    return text


@declared_tool("echo-refused", _REFUSING_GATE)
def echo_refused(environment: GateEnvironment, text: str) -> str:
    _received.append(environment)
    return text


@declared_tool("echo-bare", _PLAIN_GATE)
def echo_bare(text: str) -> str:
    return text


_TOOLS = (echo_field, echo_context, echo_async, echo_refused, echo_bare)


def _server_for(environment: GateEnvironment) -> BuiltServer:
    server = create_server()
    gates: dict[str, ToolGate] = {}
    register_declared_tools(server, environment, gates, _TOOLS)
    return BuiltServer(server, gates)


def _call(server: MCPServer, name: str, arguments: dict[str, object]) -> CallToolResult:
    result = asyncio.run(server.call_tool(name, arguments))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_a_declared_tool_is_returned_unchanged_and_keeps_its_signature() -> None:
    assert list(inspect.signature(echo_field).parameters) == ["environment", "text"]
    assert echo_field.__name__ == "echo_field"


def test_binding_an_environment_removes_it_from_the_tool_signature() -> None:
    environment = fail_closed_environment()

    bound = bind_environment(echo_field, environment)

    assert list(inspect.signature(bound).parameters) == ["text"]
    assert bound("abcd") == "abcd"
    assert _received[-1] is environment


def test_binding_leaves_a_tool_without_an_environment_parameter_unchanged() -> None:
    assert bind_environment(echo_bare, fail_closed_environment()) is echo_bare


def test_the_input_schema_of_a_bound_tool_has_no_environment_or_context() -> None:
    built = _server_for(fail_closed_environment())

    schemas = {tool.name: tool.input_schema for tool in asyncio.run(built.server.list_tools())}

    assert set(schemas) == {name for name in schemas if name.startswith("echo-")}
    for name, schema in schemas.items():
        assert "environment" not in schema["properties"], name
        assert "ctx" not in schema["properties"], name
    assert schemas["echo-field"]["properties"]["text"]["minLength"] == 3
    assert schemas["echo-field"]["properties"]["text"]["description"] == "the text"
    assert schemas["echo-field"]["required"] == ["text"]


def test_a_field_constraint_is_still_enforced_after_binding() -> None:
    built = _server_for(fail_closed_environment())

    with pytest.raises(ToolError, match="at least 3 characters"):
        _call(built.server, "echo-field", {"text": "ab"})
    assert _call(built.server, "echo-field", {"text": "abc"}).is_error is False


def test_a_bound_tool_receives_the_environment_the_server_was_built_with() -> None:
    environment = fail_closed_environment()
    built = _server_for(environment)

    result = _call(built.server, "echo-field", {"text": "abc"})

    assert _text(result) == "abc"
    assert _received[-1] is environment


def test_a_bound_async_tool_is_awaited() -> None:
    environment = fail_closed_environment()
    built = _server_for(environment)

    result = _call(built.server, "echo-async", {"text": "later"})

    assert _text(result) == "later"
    assert _received[-1] is environment


def test_a_context_parameter_is_injected_beside_the_environment() -> None:
    built = _server_for(fail_closed_environment())

    result = _call(built.server, "echo-context", {"text": "here"})

    assert result.is_error is False
    assert _text(result) == "here:Context"


def test_the_role_gate_runs_before_a_bound_tool_body() -> None:
    configure()
    touched.clear()
    environment = fail_closed_environment(resolve_actor=_no_actor)
    _received.clear()
    built = _server_for(environment)

    result = _call(built.server, "echo-refused", {"text": "x"})

    assert _text(result) == _NO_CALLER
    assert _received == []
    assert touched == []


def _no_actor(_config: ServiceConfig) -> None:
    return None


def test_two_servers_in_one_process_keep_their_own_environment_and_gates() -> None:
    first_env = fail_closed_environment()
    second_env = fail_closed_environment()
    first = _server_for(first_env)
    second = _server_for(second_env)

    _call(first.server, "echo-async", {"text": "a"})
    assert _received[-1] is first_env
    _call(second.server, "echo-async", {"text": "b"})
    assert _received[-1] is second_env
    assert first.server is not second.server
    assert first.gates is not second.gates
    assert set(first.gates) == set(second.gates)


def test_a_declared_tool_records_the_name_and_gate_it_registers_with() -> None:
    built = _server_for(fail_closed_environment())

    assert built.gates["echo-field"] is _PLAIN_GATE
    assert built.gates["echo-refused"] is _REFUSING_GATE
    assert "echo-field" not in mcp_server.TOOL_GATES


def test_build_server_can_run_more_than_once_in_a_process() -> None:
    environment = fail_closed_environment()

    first = mcp_server.build_server(environment)
    second = mcp_server.build_server(environment)

    assert first.server is not second.server
    assert first.gates is not second.gates
    assert first.gates is not mcp_server.TOOL_GATES


def test_build_server_registers_each_group_on_the_server_it_builds() -> None:
    registrations: list[tuple[MCPServer, GateEnvironment, dict[str, ToolGate]]] = []

    def register_throwaway_tools(
        server: MCPServer, environment: GateEnvironment, gates: dict[str, ToolGate]
    ) -> None:
        registrations.append((server, environment, gates))
        register_declared_tools(server, environment, gates, (echo_async,))

    groups = ((mcp_server, register_throwaway_tools),)
    environment = fail_closed_environment()

    first = mcp_server.build_server(environment, groups=groups)
    second = mcp_server.build_server(environment, groups=groups)

    assert registrations == [
        (first.server, environment, first.gates),
        (second.server, environment, second.gates),
    ]
    assert set(first.gates) == {"echo-async"}


def test_build_test_server_builds_from_overrides_and_fails_closed() -> None:
    touched.clear()
    server = build_test_server()

    assert isinstance(server, MCPServer)
    assert server is not mcp_server.server


def test_every_group_registration_function_follows_the_naming_and_signature_convention() -> None:
    for module, register in mcp_server.TOOL_GROUP_MODULES:
        if register is None:
            continue
        stem = module.__name__.rsplit(".", 1)[-1]
        assert register.__name__ == f"register_{stem}"
        assert list(inspect.signature(register).parameters) == ["server", "environment", "gates"]


def test_the_cypher_group_registers_its_tools_gates_and_resource_on_a_built_server() -> None:
    built = mcp_server.build_server(fail_closed_environment())

    tools = asyncio.run(built.server.list_tools())
    resources = asyncio.run(built.server.list_resources())

    assert {"run-cypher", "get-domain-concepts"} <= {t.name for t in tools}
    assert built.gates["run-cypher"] == PLAIN_READ_GATE
    assert built.gates["get-domain-concepts"] == PLAIN_READ_GATE
    assert [str(r.uri) for r in resources] == ["psdomain://concepts"]


def test_a_cypher_call_on_a_fail_closed_server_never_opens_a_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    opened: list[tuple[str, int]] = []

    def _connect(host: str, port: int) -> object:
        opened.append((host, port))
        raise AssertionError

    monkeypatch.setattr(falkordb_client, "connect", _connect)
    touched.clear()

    result = asyncio.run(
        build_test_server().call_tool("run-cypher", {"query": "MATCH (n) RETURN n"})
    )

    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert block.text == "error: the policy graph database is not reachable"
    assert opened == []
    assert touched == ["graph_factory"]


_ACCESS_AUDIT_GATES = {
    "list-access-roles": ACCESS_ROLE_MANAGEMENT_GATE,
    "grant-access-role": ACCESS_ROLE_CHANGE_GATE,
    "revoke-access-role": ACCESS_ROLE_CHANGE_GATE,
    "list-audit-events": ACCESS_ROLE_MANAGEMENT_GATE,
    "verify-audit-log": ACCESS_ROLE_MANAGEMENT_GATE,
}


def test_the_access_audit_group_registers_its_tools_and_gates_on_a_built_server() -> None:
    built = mcp_server.build_server(fail_closed_environment())

    tools = asyncio.run(built.server.list_tools())

    assert {name: built.gates[name] for name in _ACCESS_AUDIT_GATES} == _ACCESS_AUDIT_GATES
    assert _ACCESS_AUDIT_GATES.keys() <= {t.name for t in tools}


def test_an_audit_override_alone_reaches_the_role_store_a_list_access_roles_call_builds() -> None:
    configure()
    audit_store = InMemoryAuditStore()
    received: list[AuditStore] = []

    def _role_factory(_config: ServiceConfig, /, *, audit_store: AuditStore) -> object:
        received.append(audit_store)
        return FakeAccessRoleStore(expected_owner=("first-caller", _ISSUER))

    server = build_test_server(
        audit_store_factory=audit_store_factory(audit_store), role_store_factory=_role_factory
    )

    with _verified_actor(sub="first-caller"):
        result = asyncio.run(server.call_tool("list-access-roles", {}))

    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    assert received
    assert all(store is audit_store for store in received)


def test_a_list_access_roles_call_on_a_fail_closed_server_touches_only_the_audit_factory() -> None:
    configure()
    touched.clear()

    with _verified_actor(sub="first-caller"), pytest.raises(ToolError):
        asyncio.run(build_test_server().call_tool("list-access-roles", {}))

    assert touched == ["audit_store_factory"]


_CATALOG_GATES = {
    "set-catalog-source": SYSTEM_ADMIN_GATE,
    "reset-catalog-source": SYSTEM_ADMIN_GATE,
    "get-catalog-source": SYSTEM_ADMIN_GATE,
    "invite-user": SYSTEM_ADMIN_GATE,
    "get-catalog-listing": PLAIN_READ_GATE,
    "restore-instrument": COMPLIANCE_OFFICER_GATE,
}


def test_the_catalog_group_registers_its_tools_and_gates_on_a_built_server() -> None:
    built = mcp_server.build_server(fail_closed_environment())

    tools = asyncio.run(built.server.list_tools())

    assert {name: built.gates[name] for name in _CATALOG_GATES} == _CATALOG_GATES
    assert _CATALOG_GATES.keys() <= {t.name for t in tools}


def test_an_audit_override_alone_reaches_the_runtime_config_store_a_get_catalog_source_call_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    audit_store = InMemoryAuditStore()
    received: list[AuditStore] = []

    def _runtime_config_factory(
        config: ServiceConfig, /, *, audit_store: AuditStore
    ) -> InMemoryRuntimeConfigStore:
        received.append(audit_store)
        return InMemoryRuntimeConfigStore(config)

    server = build_test_server(
        audit_store_factory=audit_store_factory(audit_store),
        runtime_config_store_factory=_runtime_config_factory,
    )

    result = asyncio.run(server.call_tool("get-catalog-source", {}))

    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    assert received
    assert all(store is audit_store for store in received)


def test_a_get_catalog_source_call_on_a_fail_closed_server_touches_only_the_audit_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    touched.clear()

    result = asyncio.run(build_test_server().call_tool("get-catalog-source", {}))

    assert isinstance(result, CallToolResult)
    assert touched == ["audit_store_factory"]


_INGESTION_GATES = {
    "ingest-regulation": COMPLIANCE_OFFICER_GATE,
    "start-ingestion": COMPLIANCE_OFFICER_GATE,
    "get-ingestion-status": COMPLIANCE_OFFICER_GATE,
    "check-regulations": COMPLIANCE_OFFICER_GATE,
    "check-instrument-ingestion-status": PLAIN_READ_GATE,
}


def test_the_ingestion_group_registers_its_tools_and_gates_on_a_built_server() -> None:
    built = mcp_server.build_server(fail_closed_environment())

    tools = asyncio.run(built.server.list_tools())

    assert {name: built.gates[name] for name in _INGESTION_GATES} == _INGESTION_GATES
    assert _INGESTION_GATES.keys() <= {t.name for t in tools}


def test_an_audit_override_alone_reaches_the_run_store_a_get_ingestion_status_call_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    audit_store = InMemoryAuditStore()
    received: list[AuditStore] = []

    def _run_store_factory(
        _config: ServiceConfig, /, *, audit_store: AuditStore
    ) -> InMemoryIngestionRunStore:
        received.append(audit_store)
        return InMemoryIngestionRunStore()

    server = build_test_server(
        audit_store_factory=audit_store_factory(audit_store),
        ingestion_run_store_factory=_run_store_factory,
    )

    result = asyncio.run(server.call_tool("get-ingestion-status", {"run_id": "unseen"}))

    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    assert received
    assert all(store is audit_store for store in received)


def test_a_get_ingestion_status_call_on_a_fail_closed_server_touches_only_the_audit_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    touched.clear()

    result = asyncio.run(build_test_server().call_tool("get-ingestion-status", {"run_id": "x"}))

    assert isinstance(result, CallToolResult)
    assert touched == ["audit_store_factory"]


def test_a_check_instrument_ingestion_status_call_opens_its_graph_through_the_environment() -> None:
    configure()
    touched.clear()

    result = asyncio.run(
        build_test_server().call_tool(
            "check-instrument-ingestion-status", {"celex_ids": ["32024R2847"]}
        )
    )

    assert isinstance(result, CallToolResult)
    assert touched == ["graph_factory"]
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert json.loads(block.text) == {"statuses": {"32024R2847": "unknown"}}


_POLICY_LIFECYCLE_GATES = {
    "create-policy-draft": POLICY_AUTHOR_GATE,
    "update-policy-draft": POLICY_DRAFT_ACCESS_GATE,
    "add-standard-to-draft": POLICY_DRAFT_ACCESS_GATE,
    "update-standard-draft": POLICY_DRAFT_ACCESS_GATE,
    "add-control-to-draft": POLICY_DRAFT_ACCESS_GATE,
    "update-control-draft": POLICY_DRAFT_ACCESS_GATE,
    "get-policy": POLICY_DRAFT_ACCESS_GATE,
    "propose-policy": POLICY_AUTHOR_GATE,
    "revert-policy-to-draft": POLICY_AUTHOR_GATE,
}


def test_the_policy_lifecycle_group_registers_its_tools_and_gates_on_a_built_server() -> None:
    built = mcp_server.build_server(fail_closed_environment())

    tools = asyncio.run(built.server.list_tools())

    assert {name: built.gates[name] for name in _POLICY_LIFECYCLE_GATES} == _POLICY_LIFECYCLE_GATES
    for name in ("approve-policy", "reject-policy"):
        assert built.gates[name].kind is GateKind.BODY
        assert built.gates[name].role is AccessRole.POLICY_MANAGER
        assert built.gates[name].bypass is BypassPolicy.LIFECYCLE_IDENTITY
    registered = {t.name for t in tools}
    assert _POLICY_LIFECYCLE_GATES.keys() | {"approve-policy", "reject-policy"} <= registered


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get-policy", {"policy_id": "POL-1"}),
        ("update-policy-draft", {"policy_id": "POL-1"}),
        ("propose-policy", {"policy_id": "POL-1"}),
        ("approve-policy", {"policy_id": "POL-1"}),
        ("create-policy-draft", {"title": "A policy"}),
    ],
)
def test_a_policy_lifecycle_call_on_a_fail_closed_server_opens_its_graph_through_the_environment(
    monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, object]
) -> None:
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    touched.clear()

    result = asyncio.run(build_test_server().call_tool(tool, arguments))

    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert block.text == "error: the policy graph database is not reachable"
    assert touched[-1] == "graph_factory"


def test_the_lifecycle_role_enforcer_runs_the_gate_it_is_given_over_the_environments_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    audit_store = InMemoryAuditStore()
    roles = FakeAccessRoleStore()

    def _role_factory(_config: ServiceConfig, /, *, audit_store: AuditStore) -> FakeAccessRoleStore:
        del audit_store
        return roles

    environment = fail_closed_environment(
        audit_store_factory=audit_store_factory(audit_store), role_store_factory=_role_factory
    )
    gate = mcp_server.build_server(fail_closed_environment()).gates["approve-policy"]
    enforce = lifecycle_role_enforcer(environment, "approve-policy", gate)

    with pytest.raises(AccessDeniedError):
        enforce(("nobody", _ISSUER))

    assert [row.action for row in audit_store.rows] == ["access.gate"]


def test_the_lifecycle_role_enforcer_refuses_a_gate_that_is_not_a_lifecycle_body_gate() -> None:
    environment = fail_closed_environment()

    with pytest.raises(ValueError, match="body gate"):
        lifecycle_role_enforcer(environment, "list-access-roles", PLAIN_READ_GATE)
    with pytest.raises(ValueError, match="lifecycle identity"):
        lifecycle_role_enforcer(environment, "a-body-gated-tool", _NON_LIFECYCLE_BODY_GATE)


_GRAPH_CLEANUP_TOOLS = (
    "find-capability-merge-candidates",
    "find-duplicate-obligations",
    "merge-capabilities",
    "merge-obligations",
    "merge-roles",
    "release-capability-governance",
    "unmerge-entities",
    "check-cleanup-approval",
)


def test_the_graph_cleanup_group_registers_its_tools_and_gates_on_a_built_server() -> None:
    built = mcp_server.build_server(fail_closed_environment())

    tools = {t.name: t for t in asyncio.run(built.server.list_tools())}

    assert {name: built.gates[name] for name in _GRAPH_CLEANUP_TOOLS} == dict.fromkeys(
        _GRAPH_CLEANUP_TOOLS, CLEANUP_GATE
    )
    assert set(_GRAPH_CLEANUP_TOOLS) <= tools.keys()
    for name in _GRAPH_CLEANUP_TOOLS:
        assert "environment" not in tools[name].input_schema["properties"], name
    assert "ctx" not in tools["merge-capabilities"].input_schema["properties"]


def test_the_cleanup_dependencies_are_the_environments_factories() -> None:
    environment = fail_closed_environment()

    dependencies = cleanup_dependencies(environment)

    assert dependencies.open_single_tenant_graph is environment.graph_factory
    assert dependencies.audit_store is environment.audit_store_factory
    assert dependencies.access_role_store == environment.access_role_store


def _role_factory_for(store: FakeAccessRoleStore) -> Callable[..., FakeAccessRoleStore]:
    def _factory(*_args: object, **_kwargs: object) -> FakeAccessRoleStore:
        return store

    return _factory


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("find-capability-merge-candidates", {}),
        ("find-duplicate-obligations", {}),
        ("merge-capabilities", {"survivor_id": "cap-a", "absorbed_id": "cap-b"}),
        ("merge-obligations", {"survivor_id": "obl-a", "absorbed_id": "obl-b"}),
        ("merge-roles", {"survivor_id": "role-a", "absorbed_id": "role-b"}),
    ],
)
def test_a_graph_cleanup_call_on_a_fail_closed_server_opens_its_graph_through_the_environment(
    tool: str, arguments: dict[str, object]
) -> None:
    configure()
    roles = FakeAccessRoleStore(expected_owner=("owner", _ISSUER))
    roles.bootstrap_first_owner(("owner", _ISSUER))
    roles.grant(
        actor=("owner", _ISSUER),
        target=("officer", _ISSUER),
        access_role=AccessRole.COMPLIANCE_OFFICER,
    )
    touched.clear()
    server = build_test_server(
        audit_store_factory=audit_store_factory(InMemoryAuditStore()),
        role_store_factory=_role_factory_for(roles),
    )

    with _verified_actor(sub="officer"):
        result = asyncio.run(server.call_tool(tool, arguments))

    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert block.text == "error: the policy graph database is not reachable"
    assert touched == ["graph_factory"]


_NEAR_MISS_TOOLS = (
    "list-near-misses",
    "resolve-near-miss",
    "merge-near-misses",
    "check-near-miss-merges",
)
_NEAR_MISS_GATES = {
    "list-near-misses": PLAIN_READ_GATE,
    "resolve-near-miss": NEAR_MISS_KEEP_SEPARATE_GATE,
    "merge-near-misses": CLEANUP_GATE,
    "check-near-miss-merges": OWN_APPROVAL_READ_GATE,
}


def test_the_near_miss_group_registers_its_tools_and_gates_on_a_built_server() -> None:
    built = mcp_server.build_server(fail_closed_environment())

    tools = {t.name: t for t in asyncio.run(built.server.list_tools())}

    assert {name: built.gates[name] for name in _NEAR_MISS_TOOLS} == _NEAR_MISS_GATES
    for name in _NEAR_MISS_TOOLS:
        assert "environment" not in tools[name].input_schema["properties"], name
    assert "ctx" not in tools["resolve-near-miss"].input_schema["properties"]


def _officer_roles() -> FakeAccessRoleStore:
    roles = FakeAccessRoleStore(expected_owner=("owner", _ISSUER))
    roles.bootstrap_first_owner(("owner", _ISSUER))
    roles.grant(
        actor=("owner", _ISSUER),
        target=("officer", _ISSUER),
        access_role=AccessRole.COMPLIANCE_OFFICER,
    )
    return roles


def _call_as_officer(server: MCPServer, tool: str, arguments: dict[str, object]) -> str:
    with _verified_actor(sub="officer"):
        result = asyncio.run(server.call_tool(tool, arguments))
    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


@pytest.mark.parametrize(
    ("tool", "arguments", "expected_touched"),
    [
        ("list-near-misses", {}, ["graph_factory"]),
        (
            "resolve-near-miss",
            {"review_id": "review_aaa", "decision": "keep-separate"},
            ["graph_factory"],
        ),
        (
            "check-near-miss-merges",
            {"pending_approval_id": "approval-1"},
            ["pending_approval_store_factory"],
        ),
    ],
)
def test_the_other_near_miss_tools_on_a_fail_closed_server_use_only_the_environment(
    tool: str, arguments: dict[str, object], expected_touched: list[str]
) -> None:
    configure()
    touched.clear()
    server = build_test_server(
        audit_store_factory=audit_store_factory(InMemoryAuditStore()),
        role_store_factory=_role_factory_for(_officer_roles()),
    )

    text = _call_as_officer(server, tool, arguments)

    assert text.startswith("error: ")
    assert touched == expected_touched
