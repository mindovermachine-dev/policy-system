"""A caller refused by the `merge-near-misses` role gate leaves exactly one `access.gate` row.

The gate is declared on the tool, so a denied officer-less caller gets the unchanged access error
and one audit row named after the tool, while a caller with no verified identity leaves none and a
permitted caller leaves none either.
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

from mcp_interface._near_miss_batch_env import DENIED, REVIEW, UNAUTHENTICATED, BatchEnv
from mcp_interface.test_access_role_tools import (
    _override,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
    _server,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
)
from ps_service.authz.models import AccessRole
from ps_service.logging import configure
from ps_service.mcp_interface.mcp_server import TOOL_GATES
from ps_service.mcp_interface.tool_gates import BypassPolicy, GateKind

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
    _override(monkeypatch, audit_store_factory=audit_store_factory(store))
    return store


@pytest.fixture
def env(
    monkeypatch: pytest.MonkeyPatch, cleanup_rig: CleanupRig, audit_store: InMemoryAuditStore
) -> BatchEnv:
    del audit_store  # patched first so the environment's role store sees it
    configure()
    return BatchEnv(monkeypatch, cleanup_rig)


def _gate_rows(store: InMemoryAuditStore) -> list[RecordedAuditRow]:
    return [row for row in store.rows if row.action == "access.gate"]


def test_merge_near_misses_is_declared_with_a_role_gate_that_refuses_the_bypass() -> None:
    gate = TOOL_GATES["merge-near-misses"]
    assert (gate.kind, gate.role, gate.bypass) == (
        GateKind.ROLE,
        AccessRole.COMPLIANCE_OFFICER,
        BypassPolicy.REFUSE,
    )


def test_a_denied_caller_writes_exactly_one_access_gate_row_named_merge_near_misses(
    env: BatchEnv, audit_store: InMemoryAuditStore
) -> None:
    text = env.merge(who=_STRANGER)

    assert text == DENIED
    (row,) = _gate_rows(audit_store)
    assert (row.actor_subject, row.actor_issuer) == _STRANGER
    assert (row.outcome, row.resource_type, row.resource_id) == (
        "rejected",
        "operation",
        "merge-near-misses",
    )
    assert row.details == {"required_role": "ComplianceOfficer"}
    assert approval_row_count(env.store) == 0


def test_a_caller_with_no_verified_identity_writes_no_access_gate_row(
    env: BatchEnv, audit_store: InMemoryAuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")

    result = asyncio.run(_server().call_tool("merge-near-misses", {"review_ids": [REVIEW]}))

    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert block.text == UNAUTHENTICATED
    assert audit_store.rows == []


def test_a_permitted_caller_writes_no_access_gate_row(
    env: BatchEnv, audit_store: InMemoryAuditStore
) -> None:
    text = env.merge(who=OFFICER)

    assert "pending_approval_id" in json.loads(text)
    assert _gate_rows(audit_store) == []


def test_the_denied_row_is_returned_by_list_audit_events_for_actor_and_action(
    env: BatchEnv,
) -> None:
    env.merge(who=_STRANGER)

    listed = call_tool(
        "list-audit-events", {"actor_subject": _STRANGER[0], "action": "access.gate"}, OWNER
    )

    events = json.loads(listed)["events"]
    assert [(e["actor_subject"], e["resource_id"]) for e in events] == [
        (_STRANGER[0], "merge-near-misses")
    ]
