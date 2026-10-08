"""MCP and REST keep-separate record the same `near_miss.resolve` row (issue #195, AC-BI-017)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fastapi.testclient import TestClient

from api._audit_fakes import InMemoryAuditStore
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
from ps_service.api.near_miss_review_orchestration import run_resolve_near_miss
from ps_service.audit import AuditContext
from ps_service.company_merge.models import ResolveOutcome
from ps_service.main import create_app

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle


def _resolve(
    graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
) -> ResolveOutcome | None:
    _ = graph
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
