"""`postgres_live`: a refused and an applied split leave `capability.split` rows in an intact chain.

The split runs through the real executor over the Graph Cleanup gateway rig, but its audit rows go
to the real `PsycopgAuditStore` on a brand-new provisioned cluster. The first approval is refused
stale (a `failed` row with its `reason_code`), the second is applied (an `applied` row with the
moved edges and snapshots). The chain is then verified the way an operator does: the
`verify-audit-log` MCP tool, plus the independent oracle.

Deselected by default -- run with `uv run pytest -m postgres_live` (see
`persistence/provisioned_postgres.py`).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest
from audit._integrity_support import assert_chain_is_intact

from graph_cleanup._gateway_fakes import SPLIT_A, SPLIT_X
from graph_cleanup._merge_runs import roles
from graph_cleanup._split_runs import SPLIT_ACTION, run_capability_split, split_row
from graph_cleanup.test_role_merge_audit_chain_live import (
    _OVERRIDES,  # pyright: ignore[reportPrivateUsage]  # reuse the role chain test's tool overrides
    _factory,  # pyright: ignore[reportPrivateUsage]  # reuse the role chain test's store factory
    _LinkedAuditStore,  # pyright: ignore[reportPrivateUsage]  # reuse the role chain test's linked store
    _owner_only,  # pyright: ignore[reportPrivateUsage]  # reuse the role chain test's owner grants
    _verify_audit_log_tool,  # pyright: ignore[reportPrivateUsage]  # reuse the role chain test's tool call
)
from ps_service.audit import PsycopgAuditStore
from ps_service.audit.models import AuditChainVerification
from ps_service.graph_cleanup.audit_actions import CapabilitySplitDetails
from ps_service.logging import configure, reset_for_tests

if TYPE_CHECKING:
    from persistence.provisioned_postgres import Provisioned

    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.audit.store import AuditStore

pytestmark = pytest.mark.postgres_live


def _a_loses_an_edge(rig: CleanupRig) -> None:
    [key] = [
        k for k in rig.graph.edges if k[0] == "REQUIRES" and k[2] == "obl_stays" and k[4] == SPLIT_A
    ]
    del rig.graph.edges[key]


def test_verify_audit_log_passes_after_a_refused_and_an_applied_split(
    cleanup_rig: CleanupRig, fresh_state_postgres_env: Provisioned, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure()
    prov = fresh_state_postgres_env
    rig = cleanup_rig
    rig.seed_capability_split_world(governed=True)
    audit = PsycopgAuditStore(prov.state_config())
    linked = _LinkedAuditStore(audit, rig)

    def _audit_store(_config: object) -> AuditStore:
        return cast("AuditStore", linked)

    deps = replace(rig.dependencies(roles()), audit_store=_audit_store)
    stale_row = split_row(rig)
    _a_loses_an_edge(rig)

    refused = run_capability_split(rig, row=stale_row, dependencies=deps)
    applied_row = split_row(rig)
    applied = run_capability_split(rig, row=applied_row, dependencies=deps)

    assert "error" in refused
    assert applied == {"x_id": SPLIT_X, "a_id": SPLIT_A, "split": True}
    assert rig.order() == ["audit:failed", "audit:applied", "log_append"]
    with prov.superuser_connect(prov.state_db) as conn:
        rows = conn.execute(
            "SELECT action, resource_type, resource_id, outcome, details "
            "FROM audit_log.audit_events ORDER BY chain_seq"
        ).fetchall()
    assert [r[:4] for r in rows] == [
        (SPLIT_ACTION, "capability", SPLIT_X, "failed"),
        (SPLIT_ACTION, "capability", SPLIT_X, "applied"),
    ]
    failed = CapabilitySplitDetails.model_validate(rows[0][4])
    assert failed.reason_code == "state_digest_mismatch"
    assert failed.approval_id == stale_row.id
    done = CapabilitySplitDetails.model_validate(rows[1][4])
    assert done.approval_id == applied_row.id
    assert done.reason_code is None
    assert done.moved_edges
    assert done.before is not None
    assert done.after is not None

    monkeypatch.setitem(_OVERRIDES, "role_store_factory", _factory(_owner_only()))
    reported = _verify_audit_log_tool()
    reset_for_tests()

    assert reported == {"intact": True, "rows_checked": 2, "head_chain_seq": 2, "first_break": None}
    assert audit.verify_integrity() == AuditChainVerification(
        intact=True, rows_checked=2, head_chain_seq=2, first_break=None
    )
    assert assert_chain_is_intact(prov) == 2
