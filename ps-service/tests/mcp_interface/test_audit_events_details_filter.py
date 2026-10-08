"""`list-audit-events` allow-listed `details` filter via the MCP tool (issue #195, AC-BI-018)."""

from __future__ import annotations

import asyncio
import json

import pytest
from audit._fakes import InMemoryAuditStore, audit_store_factory
from mcp.types import CallToolResult, TextContent

import ps_service.ingestion_runs.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers ingestion_run.* actions
from mcp_interface.test_catalog_source_authz_gate import (
    _SYSTEM_OWNER_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _fake_store_factory,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _seeded_store,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _verified_actor,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.audit import AuditDetails, register_audit_action, register_audit_resource_type
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server


class _RestoreLikeDetails(AuditDetails):
    """Stand-in for the `instrument.restore` model (registered by a later slice)."""

    instrument_id: str


_RESTORE_LIKE_ACTION = "test_details_filter.restore_like"
register_audit_action(_RESTORE_LIKE_ACTION, _RestoreLikeDetails)
register_audit_resource_type("test_details_filter_instrument")

_ACTOR = ("actor-sub", "https://issuer.example.com/")


@pytest.fixture(name="audit_store", autouse=True)
def _audit_store_fixture(monkeypatch: pytest.MonkeyPatch) -> InMemoryAuditStore:  # pyright: ignore[reportUnusedFunction]  # autouse + injected by name
    store = InMemoryAuditStore()
    monkeypatch.setattr(mcp_server, "PsycopgAuditStore", audit_store_factory(store))
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(_seeded_store()))
    return store


def _seed(store: InMemoryAuditStore) -> None:
    for run_id, celex in (("run-a", "32024R2847"), ("run-b", "32016R0679")):
        store.record_standalone(
            actor_subject=_ACTOR[0],
            actor_issuer=_ACTOR[1],
            action="ingestion_run.submit",
            resource_type="ingestion_run",
            resource_id=run_id,
            outcome="applied",
            details={
                "celex": celex,
                "short_name": "x",
                "status": "started",
                "trigger": "sync_ingest",
            },
        )
    store.record_standalone(
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        action="ingestion_run.complete",
        resource_type="ingestion_run",
        resource_id="run-a",
        outcome="applied",
        details={
            "status": "succeeded",
            "celex": "32024R2847",
            "trigger": "sync_ingest",
            "regulatory_instrument_id": "inst-1",
            "new_obligations": 0,
            "new_capabilities": 0,
            "matched_capabilities": 0,
        },
    )
    store.record_standalone(
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        action=_RESTORE_LIKE_ACTION,
        resource_type="test_details_filter_instrument",
        resource_id="inst-9",
        outcome="applied",
        details={"instrument_id": "inst-9"},
    )


def _call(**kwargs: object) -> CallToolResult:
    configure()
    with _verified_actor(sub=_SYSTEM_OWNER_SUBJECT):
        result = asyncio.run(mcp_server.server.call_tool("list-audit-events", kwargs))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _resource_ids(result: CallToolResult) -> list[str]:
    assert result.is_error is False
    events: list[dict[str, object]] = json.loads(_text(result))["events"]
    return [str(event["resource_id"]) for event in events]


def test_list_audit_events_tool_accepts_details_filter(audit_store: InMemoryAuditStore) -> None:
    _seed(audit_store)

    result = _call(details={"celex": "32024R2847"})

    # D-F: the complete row carries the CELEX too, so a CELEX search finds both rows of a run.
    assert _resource_ids(result) == ["run-a", "run-a"]


def test_list_audit_events_tool_rejects_unknown_details_key_with_error_prefix(
    audit_store: InMemoryAuditStore,
) -> None:
    _seed(audit_store)

    result = _call(details={"foo": "x"})

    assert result.is_error is False
    assert _text(result).startswith("error: ")


def test_list_audit_events_tool_details_filter_by_celex_combines_with_action(
    audit_store: InMemoryAuditStore,
) -> None:
    _seed(audit_store)

    result = _call(details={"celex": "32016R0679"}, action="ingestion_run.submit")

    assert _resource_ids(result) == ["run-b"]


def test_list_audit_events_tool_details_by_regulatory_instrument_id_returns_the_complete_row(
    audit_store: InMemoryAuditStore,
) -> None:
    _seed(audit_store)

    result = _call(details={"regulatory_instrument_id": "inst-1"})

    assert _resource_ids(result) == ["run-a"]
    assert json.loads(_text(result))["events"][0]["action"] == "ingestion_run.complete"


def test_list_audit_events_tool_details_by_instrument_id_returns_only_matching_rows(
    audit_store: InMemoryAuditStore,
) -> None:
    _seed(audit_store)

    result = _call(details={"instrument_id": "inst-9"})

    assert _resource_ids(result) == ["inst-9"]
