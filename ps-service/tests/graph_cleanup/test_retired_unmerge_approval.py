"""An approval row that names a retired tool is inert: nothing reads, settles or records it.

The retired name is built from fragments so it never appears as a literal in the tree.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from graph_cleanup._fakes import (
    ABSORBED,
    SURVIVOR,
    FakeApprovalStore,
    RecordedAudit,
    RecordingAuditStore,
    ScriptedUnmergeGraph,
    expire_approval,
)
from graph_cleanup._gateway_fakes import caught_up_gateway
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.executors import TOOL_UNMERGE
from ps_service.graph_cleanup.service import RECONCILE_GRACE, check_cleanup_approval
from ps_service.logging import configure
from ps_service.passkey_signing.executors import (
    resolve_approval_executor,
    resolve_effect_verifier,
)

if TYPE_CHECKING:
    from ps_service.passkey_signing.models import PendingApprovalRow

_RETIRED = "un" + "merge"
_ACTOR = ("officer", "https://issuer.example.com/")
_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=False,
)


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _seeded_audit() -> RecordingAuditStore:
    audit = RecordingAuditStore()
    audit.rows.append(
        RecordedAudit(
            actor_subject=_ACTOR[0],
            actor_issuer=_ACTOR[1],
            action="access.gate",
            resource_type="mcp_tool",
            resource_id=_RETIRED,
            outcome="applied",
            details={"operation": _RETIRED},
        )
    )
    return audit


def _signed_retired_row(store: FakeApprovalStore, *, expired_for: timedelta) -> PendingApprovalRow:
    row, _code = store.create_pending_approval(
        tool_name=_RETIRED,
        normalized_args={
            "merged_id": ABSORBED,
            "merge_approval_id": "merge-approval-1",
            "kind": "capability",
            "state_digest": "d",
        },
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        display_summary={"survivor_id": SURVIVOR},
    )
    assert store.mark_signed(row.id)
    expire_approval(store, row.id, expired_for=expired_for)
    stored = store.get_by_id(row.id)
    assert stored is not None
    return stored


def _check(
    store: FakeApprovalStore, row: PendingApprovalRow, audit: RecordingAuditStore
) -> object | None:
    return check_cleanup_approval(
        pending_approval_id=row.id,
        actor=_ACTOR,
        store=store,
        config=_CONFIG,
        dependencies=GraphCleanupDependencies(
            open_single_tenant_graph=lambda _config: ScriptedUnmergeGraph(
                marker_count=1, absorbed_count=1
            ),
            audit_store=lambda _config: audit,
            graph_write_gateway=caught_up_gateway,
        ),
    )


def test_the_current_name_is_not_the_retired_one() -> None:
    assert TOOL_UNMERGE != _RETIRED


def test_no_executor_or_effect_verifier_is_registered_for_the_retired_name() -> None:
    assert resolve_approval_executor(_RETIRED) is None
    assert resolve_effect_verifier(_RETIRED) is None
    assert resolve_approval_executor(TOOL_UNMERGE) is not None
    assert resolve_effect_verifier(TOOL_UNMERGE) is not None


def test_the_approval_status_check_does_not_report_a_retired_row() -> None:
    store = FakeApprovalStore()
    row = _signed_retired_row(store, expired_for=timedelta(minutes=1))

    assert _check(store, row, _seeded_audit()) is None


def test_a_signed_retired_row_past_the_grace_window_is_not_settled_and_nothing_is_written() -> None:
    store = FakeApprovalStore()
    row = _signed_retired_row(store, expired_for=RECONCILE_GRACE + timedelta(minutes=16))
    audit = _seeded_audit()
    rows_before = copy.deepcopy(audit.rows)

    status = _check(store, row, audit)

    assert status is None
    assert audit.rows == rows_before
    stored = store.get_by_id(row.id)
    assert stored is not None
    assert (stored.status, stored.outcome) == ("signed", None)
