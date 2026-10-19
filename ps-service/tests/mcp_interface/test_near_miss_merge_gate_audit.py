"""The `merge` branch of `near_misses_resolve` is role-gated and audited; `keep-separate` is not.

`near_misses_resolve` declares a body gate: only the merge decision needs `ComplianceOfficer`, so
the tool body runs the shared request gate for that branch. A denied merge returns the unchanged
error string and writes one `access.gate` row named after the tool; a keep-separate call needs no
role and so never writes one.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from audit._fakes import InMemoryAuditStore, RecordedAuditRow, audit_store_factory
from graph_cleanup._fakes import approval_row_count
from graph_cleanup._gateway_fakes import CleanupRig
from graph_cleanup._mcp_calls import call_tool
from graph_cleanup._merge_runs import OFFICER, OWNER
from graph_gateway._fakes import close_all_rigs
from mcp.types import CallToolResult, TextContent

from mcp_interface.test_near_miss_merge_via_cleanup import (
    _DENIED,  # pyright: ignore[reportPrivateUsage]  -- same fixtures as the merge-via-cleanup file
    _REVIEW,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _UNAUTHENTICATED,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _Env,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.authz.models import AccessRole
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.tool_gates import TOOL_GATES, BypassPolicy, GateKind

if TYPE_CHECKING:
    from collections.abc import Iterator

_STRANGER = ("stranger", "https://issuer.example.com/")


@pytest.fixture
def cleanup_rig() -> Iterator[CleanupRig]:
    rig = CleanupRig()
    yield rig
    close_all_rigs()


@pytest.fixture
def audit_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryAuditStore:
    store = InMemoryAuditStore()
    monkeypatch.setattr(mcp_server, "PsycopgAuditStore", audit_store_factory(store))
    return store


@pytest.fixture
def env(
    monkeypatch: pytest.MonkeyPatch, cleanup_rig: CleanupRig, audit_store: InMemoryAuditStore
) -> _Env:
    del audit_store  # patched first so the environment's role store sees it
    configure()
    return _Env(monkeypatch, cleanup_rig)


def _gate_rows(store: InMemoryAuditStore) -> list[RecordedAuditRow]:
    return [row for row in store.rows if row.action == "access.gate"]


def test_near_misses_resolve_declares_a_body_gate_that_refuses_the_bypass() -> None:
    gate = TOOL_GATES["near_misses_resolve"]
    assert (gate.kind, gate.role, gate.bypass) == (
        GateKind.BODY,
        AccessRole.COMPLIANCE_OFFICER,
        BypassPolicy.REFUSE,
    )


def test_merge_decision_denial_writes_one_access_gate_row_named_near_misses_resolve(
    env: _Env, audit_store: InMemoryAuditStore
) -> None:
    text = env.resolve_merge(who=_STRANGER)
    assert text == _DENIED
    (row,) = _gate_rows(audit_store)
    assert (row.actor_subject, row.actor_issuer) == _STRANGER
    assert (row.outcome, row.resource_type, row.resource_id) == (
        "rejected",
        "operation",
        "near_misses_resolve",
    )
    assert row.details == {"required_role": "ComplianceOfficer"}
    assert approval_row_count(env.store) == 0


def test_merge_decision_without_a_real_session_writes_no_access_gate_row(
    env: _Env, audit_store: InMemoryAuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    result = asyncio.run(
        mcp_server.server.call_tool(
            "near_misses_resolve", {"review_id": _REVIEW, "decision": "merge"}
        )
    )
    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert block.text == _UNAUTHENTICATED
    assert audit_store.rows == []


def test_permitted_merge_decision_writes_no_access_gate_row(
    env: _Env, audit_store: InMemoryAuditStore
) -> None:
    text = env.resolve_merge(who=OFFICER)
    assert "pending_approval_id" in json.loads(text)
    assert _gate_rows(audit_store) == []


def test_denied_merge_row_is_returned_by_list_audit_events_for_actor_and_action(
    env: _Env,
) -> None:
    env.resolve_merge(who=_STRANGER)
    listed = call_tool(
        "list-audit-events", {"actor_subject": _STRANGER[0], "action": "access.gate"}, OWNER
    )
    events = json.loads(listed)["events"]
    assert [(e["actor_subject"], e["resource_id"]) for e in events] == [
        (_STRANGER[0], "near_misses_resolve")
    ]
