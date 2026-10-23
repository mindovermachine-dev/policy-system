"""The obligation-unmerge flow end to end: preview, signature, executor, gateway group.

One continuity test per flow. Everything real runs: the `unmerge` MCP tool creates the
approval, the signing router verifies the passkey assertion and runs the registered executor, the
executor writes ONE gateway group, `check-cleanup-approval` reports the outcome and
`list-audit-events` returns the `applied` row. Only the graph (the gateway rig's in-memory log and
FalkorDB fakes), the stores and the role grants are fakes.

Messages, outcomes and audit-row shape are asserted directly, and so is the join: the group's
`audit_event_id` is the id `list-audit-events` returns for the `applied` row.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from passkey_signing._harness import ACTOR_ISSUER, ACTOR_SUBJECT

# Imported so pytest finds the flow's own `world` fixture (the sweep does the same).
from passkey_signing.test_graph_cleanup_unmerge_obligation_signing import world

from graph_cleanup._fakes import OBL_ABSORBED, OBL_SURVIVOR
from graph_cleanup._mcp_calls import call_tool, factory
from ps_service.audit.models import AuditEventRow
from ps_service.mcp_interface import tool_core

if TYPE_CHECKING:
    import pytest
    from passkey_signing.test_graph_cleanup_unmerge_obligation_signing import (
        _World,  # pyright: ignore[reportPrivateUsage]
    )

__all__ = ["world"]

_OWNER = ("owner", "https://issuer.example.com/")


def test_preview_sign_execute_one_group_and_the_audit_row_it_is_linked_to(
    monkeypatch: pytest.MonkeyPatch, world: _World
) -> None:
    monkeypatch.setattr(tool_core, "PsycopgPendingApprovalStore", factory(world.pending))
    monkeypatch.setattr(tool_core, "PsycopgAccessRoleStore", factory(world.roles))
    monkeypatch.setattr(tool_core, "PsycopgAuditStore", factory(world.audit))
    officer = (ACTOR_SUBJECT, ACTOR_ISSUER)

    # 1. The MCP tool previews and creates the approval; nothing is written yet.
    created = json.loads(call_tool("unmerge", {"merged_id": OBL_ABSORBED}, officer))
    assert created["preview"]["kind"] == "obligation"
    assert created["preview"]["survivor_id"] == OBL_SURVIVOR
    assert world.rig.logged_groups == []
    row = world.pending.get_by_id(created["pending_approval_id"])
    assert row is not None
    code = created["approval_url"].rsplit("#", 1)[1]

    # 2. The passkey signature runs the executor: audit row first, then ONE group.
    response = world.sign(row, code)
    assert response.status_code == 200
    assert response.json() == {
        "status": "signed",
        "merged_id": OBL_ABSORBED,
        "survivor_id": OBL_SURVIVOR,
        "unmerged": True,
        "survivor_added_edges": [],
    }
    assert world.rig.order() == ["audit:applied", "log_append"]
    [group] = world.rig.logged_groups
    [recorded] = world.audit.rows
    assert recorded.action == "obligation.unmerge"
    assert recorded.outcome == "applied"
    [event_id] = world.audit.event_ids
    assert world.rig.store.links[group.group_id] == event_id

    # 3. list-audit-events returns the applied row, by the id the group is linked to.
    merge_row = world.audit.history
    world.audit.history = [
        AuditEventRow(
            id=event_id,
            occurred_at=datetime.now(UTC),
            actor_subject=recorded.actor_subject,
            actor_issuer=recorded.actor_issuer,
            action=recorded.action,
            resource_type=recorded.resource_type,
            resource_id=recorded.resource_id,
            outcome="applied",
            details=recorded.details,
        )
    ]
    listed = json.loads(call_tool("list-audit-events", {"action": "obligation.unmerge"}, _OWNER))
    [event] = listed["events"]
    assert event["id"] == event_id
    assert (event["action"], event["resource_type"], event["outcome"]) == (
        "obligation.unmerge",
        "obligation",
        "applied",
    )
    assert event["resource_id"] == OBL_ABSORBED
    assert event["details"]["approval_id"] == row.id
    assert event["details"]["reverses_approval_id"] == "merge-approval-1"
    assert set(event["details"]) >= {"survivor_id", "absorbed_id", "before", "after"}
    assert merge_row  # the merge's own row stayed the history the unmerge was planned from

    # 4. check-cleanup-approval reports the finished approval as it always did.
    status = json.loads(
        call_tool("check-cleanup-approval", {"pending_approval_id": row.id}, officer)
    )
    assert status["tool_name"] == "unmerge"
    assert status["status"] == "signed"
    assert status["outcome"] == {
        "merged_id": OBL_ABSORBED,
        "survivor_id": OBL_SURVIVOR,
        "unmerged": True,
        "survivor_added_edges": [],
    }
