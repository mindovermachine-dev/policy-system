"""The declarative MCP role gate, proven end to end on `set-catalog-source`.

A denied call returns the tool's unchanged error string, writes one `access.gate` audit row, and
that row is readable back through the real `list-audit-events` tool.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from audit._fakes import InMemoryAuditStore, audit_store_factory
from mcp.server import MCPServer

from mcp_interface.test_catalog_source_authz_gate import (
    _ACCESS_DENIED_MESSAGE,  # pyright: ignore[reportPrivateUsage]  -- reuse this file family's fixtures verbatim
    _NON_ADMIN_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _SYSTEM_OWNER_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _call,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _fake_store_factory,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _seeded_store,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _text,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _verified_actor,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from mcp_interface.test_catalog_source_skills import (
    _OVERRIDE_URL,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _install_store,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.audit.errors import AuditPostgresUnavailableError
from ps_service.authz.models import AccessRole
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.tool_gates import TOOL_GATES, BypassPolicy, GateKind, ToolGate

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    type ReadLines = Callable[[Path], list[dict[str, object]]]

_ISSUER = "https://issuer.example.com/"
_TOOL = "set-catalog-source"
_ARGS: dict[str, object] = {"url": _OVERRIDE_URL}
_UNAUTHENTICATED_MESSAGE = "error: this action requires a real authenticated caller"


@pytest.fixture(name="audit_store")
def _audit_store_fixture(monkeypatch: pytest.MonkeyPatch) -> InMemoryAuditStore:  # pyright: ignore[reportUnusedFunction]  # injected by name
    configure()
    store = InMemoryAuditStore()
    monkeypatch.setattr(mcp_server, "PsycopgAuditStore", audit_store_factory(store))
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(_seeded_store()))
    return store


def _gate_rows(store: InMemoryAuditStore) -> list[object]:
    return [row for row in store.rows if row.action == "access.gate"]


def test_denied_set_catalog_source_returns_the_unchanged_access_denied_error(
    audit_store: InMemoryAuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    overrides = _install_store(monkeypatch)
    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        result = _call(_TOOL, _ARGS)
    assert result.is_error is False
    assert _text(result) == _ACCESS_DENIED_MESSAGE
    assert overrides.rows == {}
    assert audit_store.rows[0].action == "access.gate"


def test_denied_set_catalog_source_writes_exactly_one_access_gate_row(
    audit_store: InMemoryAuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_store(monkeypatch)
    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        _call(_TOOL, _ARGS)
    assert len(audit_store.rows) == 1
    assert len(_gate_rows(audit_store)) == 1


def test_access_gate_row_names_the_denied_principal_the_tool_and_the_required_role(
    audit_store: InMemoryAuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_store(monkeypatch)
    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        _call(_TOOL, _ARGS)
    (row,) = audit_store.rows
    assert (row.actor_subject, row.actor_issuer) == (_NON_ADMIN_SUBJECT, _ISSUER)
    assert (row.action, row.outcome) == ("access.gate", "rejected")
    assert (row.resource_type, row.resource_id) == ("operation", _TOOL)
    assert row.details == {"required_role": "SystemAdmin"}


def test_denied_row_is_returned_by_list_audit_events_for_the_actor_and_action_filters(
    audit_store: InMemoryAuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_store(monkeypatch)
    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        _call(_TOOL, _ARGS)
    with _verified_actor(sub=_SYSTEM_OWNER_SUBJECT):
        listed = _call(
            "list-audit-events", {"actor_subject": _NON_ADMIN_SUBJECT, "action": "access.gate"}
        )
    events = json.loads(_text(listed))["events"]
    assert [(e["actor_subject"], e["action"], e["resource_id"]) for e in events] == [
        (_NON_ADMIN_SUBJECT, "access.gate", _TOOL)
    ]
    assert len(audit_store.rows) == 1


def test_denial_survives_an_unwritable_audit_store(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    store = InMemoryAuditStore(
        fail_on_action={"access.gate": AuditPostgresUnavailableError("host=db.internal")}
    )
    monkeypatch.setattr(mcp_server, "PsycopgAuditStore", audit_store_factory(store))
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(_seeded_store()))
    _install_store(monkeypatch)
    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        result = _call(_TOOL, _ARGS)
    assert _text(result) == _ACCESS_DENIED_MESSAGE
    assert store.rows == []


def test_unauthenticated_call_returns_the_unchanged_message_and_writes_no_row(
    audit_store: InMemoryAuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PS_SERVICE_LOCAL_TEST_BYPASS", raising=False)
    _install_store(monkeypatch)
    result = _call(_TOOL, _ARGS)
    assert _text(result) == _UNAUTHENTICATED_MESSAGE
    assert audit_store.rows == []


def test_permitted_caller_writes_no_access_gate_row(
    audit_store: InMemoryAuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    roles = _seeded_store()
    roles.grant(
        actor=(_SYSTEM_OWNER_SUBJECT, _ISSUER),
        target=("granted-admin", _ISSUER),
        access_role=AccessRole.SYSTEM_ADMIN,
    )
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(roles))
    _install_store(monkeypatch)
    with _verified_actor(sub="granted-admin"):
        result = _call(_TOOL, _ARGS)
    assert json.loads(_text(result)) == {"url": _OVERRIDE_URL, "source": "override"}
    assert _gate_rows(audit_store) == []


def test_local_test_bypass_skips_the_gate_and_writes_no_row(
    audit_store: InMemoryAuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _install_store(monkeypatch)
    result = _call(_TOOL, _ARGS)
    assert json.loads(_text(result)) == {"url": _OVERRIDE_URL, "source": "override"}
    assert _gate_rows(audit_store) == []


def test_denied_call_still_emits_started_and_failed_log_entries(
    audit_store: InMemoryAuditStore, monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    emitter = configure()
    _install_store(monkeypatch)
    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        _call(_TOOL, _ARGS)
    emitter.flush()
    lines = [
        line
        for line in read_lines(resolve_default_log_path())
        if line.get("component") == "mcp_interface" and line.get("action") == "set_catalog_source"
    ]
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    assert len(audit_store.rows) == 1


def _input_schema(server: MCPServer, name: str) -> dict[str, object]:
    tools = asyncio.run(server.list_tools())
    return next(tool.input_schema for tool in tools if tool.name == name)


def test_wrapped_tool_input_schema_is_identical_to_the_unwrapped_schema() -> None:
    original = mcp_server.set_catalog_source.__wrapped__  # pyright: ignore[reportFunctionMemberAccess]  # `functools.wraps` sets `__wrapped__` on the gated tool
    reference = MCPServer(name="reference")
    reference.tool(name=_TOOL)(original)
    assert _input_schema(mcp_server.server, _TOOL) == _input_schema(reference, _TOOL)


def test_gate_declaration_records_operation_role_and_bypass_policy() -> None:
    assert TOOL_GATES[_TOOL] == ToolGate(
        kind=GateKind.ROLE,
        role=AccessRole.SYSTEM_ADMIN,
        bypass=BypassPolicy.SKIP,
        unauthenticated_message=_UNAUTHENTICATED_MESSAGE,
    )
