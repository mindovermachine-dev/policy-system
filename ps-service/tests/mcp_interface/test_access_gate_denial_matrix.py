"""The generic denial matrix: every role-gated MCP tool, one parametrized contract.

For each tool whose declaration is a `ROLE` gate, a caller without the role gets the tool's
unchanged `error: ...` string and exactly one `access.gate` audit row, an unauthenticated call
gets the tool's own message and no row, and the tools that refuse the local-test bypass keep
refusing it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest
from audit._fakes import InMemoryAuditStore, audit_store_factory
from curated_source._fakes import InMemoryRuntimeConfigStore

from mcp_interface.test_access_role_tools import (
    _override,  # pyright: ignore[reportPrivateUsage]  -- reuse the access-role tool tests' override helpers
    _server,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from mcp_interface.test_catalog_source_authz_gate import (
    _ACCESS_DENIED_MESSAGE,  # pyright: ignore[reportPrivateUsage]  -- reuse this file family's fixtures verbatim
    _NON_ADMIN_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _fake_store_factory,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _seeded_store,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _text,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _verified_actor,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from mcp_interface.test_catalog_source_skills import (
    _OVERRIDE_URL,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _app_config,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.authz.models import AccessRole
from ps_service.logging import configure
from ps_service.mcp_interface.mcp_server import TOOL_GATES
from ps_service.mcp_interface.tool_gates import BypassPolicy, GateKind

if TYPE_CHECKING:
    from mcp.types import CallToolResult

    from ps_service.config import ServiceConfig

_ISSUER = "https://issuer.example.com/"
_DEFAULT_UNAUTHENTICATED = "error: this action requires a real authenticated caller"
_ROLE_MANAGEMENT_UNAUTHENTICATED = (
    "error: access-role management requires a real authenticated caller"
)
_CLEANUP_UNAUTHENTICATED = "error: graph cleanup requires a real authenticated caller"

_CELEX_ARGS: dict[str, object] = {"celex": "32024R2847", "short_name": "CRA"}

# tool -> (valid dummy arguments, message returned to an unauthenticated caller)
_ROLE_TOOLS: dict[str, tuple[dict[str, object], str]] = {
    "set-catalog-source": ({"url": _OVERRIDE_URL}, _DEFAULT_UNAUTHENTICATED),
    "reset-catalog-source": ({}, _DEFAULT_UNAUTHENTICATED),
    "get-catalog-source": ({}, _DEFAULT_UNAUTHENTICATED),
    "invite-user": ({"email": "new.user@example.com"}, _DEFAULT_UNAUTHENTICATED),
    "ingest-regulation": (_CELEX_ARGS, _DEFAULT_UNAUTHENTICATED),
    "check-regulations": ({}, _DEFAULT_UNAUTHENTICATED),
    "restore-instrument": ({"instrument_id": "CRA-1"}, _DEFAULT_UNAUTHENTICATED),
    "start-ingestion": (_CELEX_ARGS, _DEFAULT_UNAUTHENTICATED),
    "get-ingestion-status": ({"run_id": "run-1"}, _DEFAULT_UNAUTHENTICATED),
    "list-access-roles": ({}, _ROLE_MANAGEMENT_UNAUTHENTICATED),
    "list-audit-events": ({}, _ROLE_MANAGEMENT_UNAUTHENTICATED),
    "verify-audit-log": ({}, _ROLE_MANAGEMENT_UNAUTHENTICATED),
    "find-capability-merge-candidates": ({}, _CLEANUP_UNAUTHENTICATED),
    "find-duplicate-obligations": ({}, _CLEANUP_UNAUTHENTICATED),
    "merge-capabilities": (
        {"survivor_id": "cap_a", "absorbed_id": "cap_b"},
        _CLEANUP_UNAUTHENTICATED,
    ),
    "merge-obligations": (
        {"survivor_id": "obl_a", "absorbed_id": "obl_b"},
        _CLEANUP_UNAUTHENTICATED,
    ),
    "merge-roles": (
        {"survivor_id": "role_a", "absorbed_id": "role_b"},
        _CLEANUP_UNAUTHENTICATED,
    ),
    "release-capability-governance": ({"capability_id": "cap_a"}, _CLEANUP_UNAUTHENTICATED),
    "unmerge-entities": ({"merged_id": "cap_b"}, _CLEANUP_UNAUTHENTICATED),
    "check-cleanup-approval": ({"pending_approval_id": "approval-1"}, _CLEANUP_UNAUTHENTICATED),
    "merge-near-misses": ({"review_ids": ["review_a"]}, _CLEANUP_UNAUTHENTICATED),
}
_REFUSING_TOOLS = sorted(
    name for name, (_args, message) in _ROLE_TOOLS.items() if message != _DEFAULT_UNAUTHENTICATED
)


def _call(tool: str, args: dict[str, object] | None = None) -> CallToolResult:
    """Call `tool` on a server built over the test environment, which carries every tool group."""
    return cast("CallToolResult", asyncio.run(_server().call_tool(tool, args or {})))


def _install_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryRuntimeConfigStore:
    """Install one in-memory runtime-config store as the server environment's store."""
    store = InMemoryRuntimeConfigStore(_app_config())

    def _factory(config: ServiceConfig, **_kwargs: object) -> InMemoryRuntimeConfigStore:
        store.config = config
        return store

    _override(monkeypatch, runtime_config_store_factory=_factory)
    return store


@pytest.fixture(name="audit_store")
def _audit_store_fixture(monkeypatch: pytest.MonkeyPatch) -> InMemoryAuditStore:  # pyright: ignore[reportUnusedFunction]  # injected by name
    configure()
    store = InMemoryAuditStore()
    _override(
        monkeypatch,
        audit_store_factory=audit_store_factory(store),
        role_store_factory=_fake_store_factory(_seeded_store()),
    )
    _install_store(monkeypatch)
    return store


def test_the_matrix_covers_exactly_the_tools_declared_as_role_gated() -> None:
    declared = {name for name, gate in TOOL_GATES.items() if gate.kind is GateKind.ROLE}
    assert declared == set(_ROLE_TOOLS)


@pytest.mark.parametrize("tool", sorted(_ROLE_TOOLS))
def test_denied_tool_returns_its_unchanged_error_and_writes_one_access_gate_row(
    tool: str, audit_store: InMemoryAuditStore
) -> None:
    args, _message = _ROLE_TOOLS[tool]
    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        result = _call(tool, args)
    assert _text(result) == _ACCESS_DENIED_MESSAGE
    assert len(audit_store.rows) == 1
    (row,) = audit_store.rows
    assert (row.action, row.outcome) == ("access.gate", "rejected")
    assert (row.actor_subject, row.actor_issuer) == (_NON_ADMIN_SUBJECT, _ISSUER)
    assert (row.resource_type, row.resource_id) == ("operation", tool)
    required = TOOL_GATES[tool].role
    assert isinstance(required, AccessRole)
    assert row.details == {"required_role": required.value}


@pytest.mark.parametrize("tool", sorted(_ROLE_TOOLS))
def test_unauthenticated_tool_call_returns_its_message_and_writes_no_row(
    tool: str, audit_store: InMemoryAuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PS_SERVICE_LOCAL_TEST_BYPASS", raising=False)
    args, message = _ROLE_TOOLS[tool]
    result = _call(tool, args)
    assert _text(result) == message
    assert audit_store.rows == []


@pytest.mark.parametrize("tool", _REFUSING_TOOLS)
def test_local_test_bypass_is_refused_with_the_authenticated_caller_message(
    tool: str, audit_store: InMemoryAuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    args, message = _ROLE_TOOLS[tool]
    result = _call(tool, args)
    assert _text(result) == message
    assert TOOL_GATES[tool].bypass is BypassPolicy.REFUSE
    assert audit_store.rows == []


def test_invite_user_denial_writes_no_user_invite_row(audit_store: InMemoryAuditStore) -> None:
    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        _call("invite-user", {"email": "new.user@example.com"})
    assert [row.action for row in audit_store.rows] == ["access.gate"]


def test_denied_ingest_regulation_starts_no_ingestion_run_row(
    audit_store: InMemoryAuditStore,
) -> None:
    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        _call("ingest-regulation", _CELEX_ARGS)
    assert [row.action for row in audit_store.rows] == ["access.gate"]


@pytest.mark.parametrize("tool", ["list-access-roles", "list-audit-events", "verify-audit-log"])
def test_denied_role_management_read_writes_exactly_one_access_gate_row(
    tool: str, audit_store: InMemoryAuditStore
) -> None:
    """The wrapper denies first, so the service-layer role assertion never adds a second row."""
    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        _call(tool, _ROLE_TOOLS[tool][0])
    assert [row.action for row in audit_store.rows] == ["access.gate"]
