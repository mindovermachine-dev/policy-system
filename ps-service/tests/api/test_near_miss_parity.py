"""MCP and REST resolve near misses through the same orchestration and record the same rows.

Keep-separate records the same `near_miss.resolve` row; a merge creates the same cleanup approval
and reports the same winner and loser.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest
from fastapi.testclient import TestClient
from graph_cleanup._fakes import ABSORBED, SURVIVOR, FakeApprovalStore
from graph_cleanup._gateway_fakes import CleanupRig
from graph_cleanup._merge_runs import CONFIG, OFFICER, roles
from graph_gateway._fakes import close_all_rigs

from api._audit_fakes import InMemoryAuditStore
from api.test_routes_near_miss_merge import (
    _Env,  # pyright: ignore[reportPrivateUsage]  -- the REST rig over a cleanup graph
)
from api.test_routes_near_misses import (
    _app_config,  # pyright: ignore[reportPrivateUsage]  -- reuse the REST-side fixtures verbatim
    _fake_dependencies,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _principal,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _record,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.api.dependencies import (
    get_principal,
    provide_audit_store,
    provide_near_miss_review_dependencies,
)
from ps_service.api.near_miss_review_orchestration import (
    build_default_near_miss_review_dependencies,
    check_near_miss_merge_approval,
    run_request_near_miss_merge,
    run_resolve_near_miss,
)
from ps_service.audit import AuditContext
from ps_service.company_merge.models import ResolveOutcome
from ps_service.graph_cleanup.executors import execute_capability_merge
from ps_service.logging import configure
from ps_service.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.graph_submission import GraphWriteTarget


def _resolve(
    graph: GraphHandle,
    review_id: str,
    decision: Literal["keep-separate", "merge"],
    target: GraphWriteTarget,
) -> ResolveOutcome | None:
    _ = (graph, target)
    return ResolveOutcome(review_id=review_id, decision=decision)


def test_mcp_and_rest_keep_separate_record_the_same_audit_row_shape() -> None:
    principal = _principal()
    dependencies, _ = _fake_dependencies((_record("review_aaa"),), resolve=_resolve)

    # REST transport.
    rest_store = InMemoryAuditStore()
    app = create_app(_app_config())
    app.dependency_overrides[provide_near_miss_review_dependencies] = lambda: dependencies
    app.dependency_overrides[provide_audit_store] = lambda: rest_store
    app.dependency_overrides[get_principal] = lambda: principal
    TestClient(app).post("/near-misses/review_aaa/resolve", json={"decision": "keep-separate"})

    # The shared orchestration the MCP tool calls, with the MCP-style context.
    mcp_store = InMemoryAuditStore()
    run_resolve_near_miss(
        "review_aaa",
        "keep-separate",
        config=_app_config(),
        dependencies=dependencies,
        audit=AuditContext((principal.sub, principal.iss), mcp_store),
    )

    assert rest_store.rows == mcp_store.rows
    assert len(rest_store.rows) == 1


@pytest.fixture
def two_rigs() -> Iterator[tuple[CleanupRig, CleanupRig]]:
    configure()
    yield CleanupRig(), CleanupRig()
    close_all_rigs()


def test_mcp_and_rest_merge_create_the_same_approval_and_report_the_same_winner_and_loser(
    monkeypatch: pytest.MonkeyPatch, two_rigs: tuple[CleanupRig, CleanupRig]
) -> None:
    rest_rig, mcp_rig = two_rigs
    rest = _Env(monkeypatch, rest_rig)

    # REST transport.
    _, created = rest.merge()
    rest_approval_id = str(created["pending_approval_id"])
    rest.sign_and_execute(rest_approval_id)
    rest_status = rest.client.get(f"/near-misses/approvals/{rest_approval_id}").json()

    # The shared orchestration the MCP tool calls, over a second, identically seeded graph.
    mcp_rig.seed_capability_merge_world()
    mcp_rig.seed_near_miss_review(
        "review_aaa",
        incoming_id=ABSORBED,
        existing_id=SURVIVOR,
        incoming_created_at="2024-01-01T00:00:00+00:00",
        existing_created_at="2020-01-01T00:00:00+00:00",
    )
    store = FakeApprovalStore()
    approval = run_request_near_miss_merge(
        "review_aaa",
        actor=OFFICER,
        acknowledge_governance_change=False,
        base_url="http://testserver",
        config=CONFIG,
        dependencies=build_default_near_miss_review_dependencies(
            open_single_tenant_graph=lambda _config: mcp_rig.read
        ),
        store=store,
    )
    store.mark_signed(approval.pending_approval_id)
    signed = store.get_by_id(approval.pending_approval_id)
    assert signed is not None
    store.set_outcome(
        approval.pending_approval_id,
        execute_capability_merge(signed, CONFIG, mcp_rig.dependencies(roles())),
    )
    mcp_status = check_near_miss_merge_approval(
        pending_approval_id=approval.pending_approval_id,
        actor=OFFICER,
        store=store,
        config=CONFIG,
        dependencies=mcp_rig.dependencies(roles()),
    )

    rest_row = rest.store.get_by_id(rest_approval_id)
    assert rest_row is not None
    assert created["preview"] == approval.preview.model_dump(mode="json")
    assert (rest_row.tool_name, rest_row.normalized_args) == (
        signed.tool_name,
        signed.normalized_args,
    )
    assert mcp_status is not None
    assert rest_status == {
        "pending_approval_id": rest_approval_id,
        "status": mcp_status.status,
        "review_id": mcp_status.review_id,
        "decision": mcp_status.decision,
        "winner_id": mcp_status.winner_id,
        "loser_id": mcp_status.loser_id,
        "error": mcp_status.error,
    }
    assert (rest_status["winner_id"], rest_status["loser_id"]) == (SURVIVOR, ABSORBED)
