"""`postgres_live`: a real Role merge leaves a `role.merge` row and an intact audit hash chain.

The merge runs through the real executor over the Graph Cleanup gateway rig, but its audit rows
go to the real `PsycopgAuditStore` on a brand-new provisioned cluster. The chain is then verified
the way an operator does: the `verify-audit-log` MCP tool (tool -> use case -> real store -> SQL
`audit_log.verify_chain()`), plus the independent oracle.

Deselected by default -- run with `uv run pytest -m postgres_live` (see
`persistence/provisioned_postgres.py`).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest
from audit._integrity_support import assert_chain_is_intact
from authz._fakes import FakeAccessRoleStore
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from graph_cleanup._fakes import ROLE_ABSORBED, ROLE_SURVIVOR
from graph_cleanup._merge_runs import role_row, roles, run_role_merge
from graph_cleanup._unmerge_runs import role_unmerge_row, run_role_unmerge
from ps_service.audit import PsycopgAuditStore
from ps_service.audit.models import AuditChainVerification
from ps_service.graph_cleanup.audit_actions import RoleMergeDetails, RoleUnmergeDetails
from ps_service.logging import configure, reset_for_tests
from ps_service.mcp_interface import mcp_server, tool_core

if TYPE_CHECKING:
    from collections.abc import Callable

    from persistence.provisioned_postgres import Provisioned

    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.audit.store import AuditStore

pytestmark = pytest.mark.postgres_live

_ISSUER = "https://issuer.example.com/"
_OWNER = ("verify-owner", _ISSUER)


class _LinkedAuditStore:
    """The real audit store, whose event ids are also registered with the rig's graph log.

    The gateway only accepts a group linked to an audit event it knows; the rig's own recording
    store does that registration, so this adapter does it for the real store.
    """

    def __init__(self, inner: PsycopgAuditStore, rig: CleanupRig) -> None:
        self._inner = inner
        self._rig = rig

    def query(self, **kwargs: object) -> object:
        return self._inner.query(**kwargs)  # type: ignore[arg-type]

    def record_standalone(self, **kwargs: object) -> None:
        self.record_standalone_with_event_id(**kwargs)

    def record_standalone_with_event_id(self, **kwargs: object) -> str:
        event_id = self._inner.record_standalone_with_event_id(**kwargs)  # type: ignore[arg-type]
        self._rig.events.append(f"audit:{kwargs['outcome']}")
        self._rig.store.audit_rows.add(event_id)
        return event_id


def _verify_audit_log_tool() -> dict[str, object]:
    token = AccessToken(
        token="t", client_id="c", scopes=[], subject=_OWNER[0], claims={"iss": _ISSUER}
    )
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        result = asyncio.run(mcp_server.server.call_tool("verify-audit-log", {}))
    finally:
        auth_context_var.reset(reset)
    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    return cast("dict[str, object]", json.loads(block.text))


def _factory(store: object) -> Callable[..., object]:
    def _make(_config: object, **_kwargs: object) -> object:
        return store

    return _make


def test_verify_audit_log_passes_after_a_real_role_merge(
    cleanup_rig: CleanupRig, fresh_state_postgres_env: Provisioned, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure()
    prov = fresh_state_postgres_env
    rig = cleanup_rig
    rig.seed_role_merge_world()
    audit = PsycopgAuditStore(prov.state_config())
    linked = _LinkedAuditStore(audit, rig)

    def _audit_store(_config: object) -> AuditStore:
        return cast("AuditStore", linked)

    deps = replace(rig.dependencies(roles()), audit_store=_audit_store)

    outcome = run_role_merge(rig, row=role_row(rig), dependencies=deps)

    assert outcome == {"survivor_id": ROLE_SURVIVOR, "absorbed_id": ROLE_ABSORBED, "merged": True}
    assert rig.order() == ["audit:applied", "log_append"]
    with prov.superuser_connect(prov.state_db) as conn:
        rows = conn.execute(
            "SELECT action, resource_type, resource_id, outcome, details "
            "FROM audit_log.audit_events ORDER BY chain_seq"
        ).fetchall()
    assert [r[:4] for r in rows] == [("role.merge", "role", ROLE_ABSORBED, "applied")]
    details = RoleMergeDetails.model_validate(rows[0][4])
    assert details.before is not None
    assert details.after is not None

    monkeypatch.setattr(tool_core, "PsycopgAccessRoleStore", _factory(_owner_only()))
    reported = _verify_audit_log_tool()
    reset_for_tests()

    assert reported == {"intact": True, "rows_checked": 1, "head_chain_seq": 1, "first_break": None}
    assert audit.verify_integrity() == AuditChainVerification(
        intact=True, rows_checked=1, head_chain_seq=1, first_break=None
    )
    assert assert_chain_is_intact(prov) == 1


def test_verify_audit_log_passes_after_a_real_role_merge_and_unmerge(
    cleanup_rig: CleanupRig, fresh_state_postgres_env: Provisioned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unmerge locates the merge in the REAL store, and both rows chain."""
    configure()
    prov = fresh_state_postgres_env
    rig = cleanup_rig
    rig.seed_role_merge_world()
    audit = PsycopgAuditStore(prov.state_config())
    linked = _LinkedAuditStore(audit, rig)

    def _audit_store(_config: object) -> AuditStore:
        return cast("AuditStore", linked)

    deps = replace(rig.dependencies(roles()), audit_store=_audit_store)
    assert run_role_merge(rig, row=role_row(rig), dependencies=deps)["merged"] is True

    row = role_unmerge_row(rig, audit=cast("AuditStore", linked))
    outcome = run_role_unmerge(rig, row=row, dependencies=deps)

    assert outcome["unmerged"] is True
    assert rig.order() == ["audit:applied", "log_append", "audit:applied", "log_append"]
    with prov.superuser_connect(prov.state_db) as conn:
        rows = conn.execute(
            "SELECT action, resource_type, resource_id, outcome, details "
            "FROM audit_log.audit_events ORDER BY chain_seq"
        ).fetchall()
    assert [r[:4] for r in rows] == [
        ("role.merge", "role", ROLE_ABSORBED, "applied"),
        ("role.unmerge", "role", ROLE_ABSORBED, "applied"),
    ]
    details = RoleUnmergeDetails.model_validate(rows[1][4])
    assert details.reverses_approval_id == "approval-1"
    assert details.before is not None
    assert details.after is not None

    monkeypatch.setattr(tool_core, "PsycopgAccessRoleStore", _factory(_owner_only()))
    reported = _verify_audit_log_tool()
    reset_for_tests()

    assert reported == {"intact": True, "rows_checked": 2, "head_chain_seq": 2, "first_break": None}
    assert assert_chain_is_intact(prov) == 2


def _owner_only() -> FakeAccessRoleStore:
    store = FakeAccessRoleStore(expected_owner=_OWNER)
    store.bootstrap_first_owner(_OWNER)
    return store
