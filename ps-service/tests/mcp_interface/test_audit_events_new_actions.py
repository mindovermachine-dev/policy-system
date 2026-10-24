"""`list-audit-events` returns the rows of the three actions the role gate work added.

Drives the real tool over a seeded in-memory store: the rows of `access.gate`, `passkey.enroll`
and `query.cypher` are found by actor, by each action, by time range and by their resource types.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from audit._fakes import InMemoryAuditStore, audit_store_factory
from mcp.types import CallToolResult, TextContent

import ps_service.authz.audit_actions  # pyright: ignore[reportUnusedImport] -- side-effect import, registers `access.gate`
import ps_service.passkey_signing.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers `passkey.enroll`
from mcp_interface.test_access_role_tools import (
    _override,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
    _server,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
)
from mcp_interface.test_catalog_source_authz_gate import (
    _SYSTEM_OWNER_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _fake_store_factory,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _seeded_store,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _verified_actor,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.audit.models import AuditEventRow, AuditQueryFilters, AuditQueryPage
from ps_service.logging import configure
from ps_service.mcp_interface.audit_actions import QUERY_CYPHER_ACTION

if TYPE_CHECKING:
    from collections.abc import Mapping

_DENIED = ("denied-sub", "https://issuer.example.com/")
_ENROLLER = ("enroller-sub", "https://issuer.example.com/")
_QUERIER = ("querier-sub", "https://issuer.example.com/")
_EARLY = datetime(2026, 3, 1, tzinfo=UTC)
_LATE = datetime(2026, 9, 1, tzinfo=UTC)


@dataclass
class _TimedAuditStore(InMemoryAuditStore):
    """The in-memory store, with a fixed `occurred_at` per row and the time filters applied."""

    times: list[datetime] | None = None

    def query(
        self, *, filters: AuditQueryFilters, cursor: str | None, page_size: int
    ) -> AuditQueryPage:
        del cursor
        times = self.times or []
        events = tuple(
            AuditEventRow(
                id=f"row-{index}",
                occurred_at=times[index],
                actor_subject=self.rows[index].actor_subject,
                actor_issuer=self.rows[index].actor_issuer,
                action=self.rows[index].action,
                resource_type=self.rows[index].resource_type,
                resource_id=self.rows[index].resource_id,
                outcome=self.rows[index].outcome,  # pyright: ignore[reportArgumentType]  # the stored row types `outcome` as `str`, the event model as a literal union
                details=self.rows[index].details,
            )
            for index in reversed(range(len(self.rows)))
            if self._matches(index, filters)
            and (filters.occurred_from is None or times[index] >= filters.occurred_from)
            and (filters.occurred_to is None or times[index] <= filters.occurred_to)
        )
        return AuditQueryPage(events=events[:page_size], next_cursor=None)

    def _matches(self, index: int, filters: AuditQueryFilters) -> bool:
        row = self.rows[index]
        return (
            (filters.action is None or row.action == filters.action)
            and (filters.resource_type is None or row.resource_type == filters.resource_type)
            and (filters.resource_id is None or row.resource_id == filters.resource_id)
            and (filters.actor_subject is None or row.actor_subject == filters.actor_subject)
            and all(row.details.get(key) == value for key, value in (filters.details or {}).items())
        )


def _write(
    store: _TimedAuditStore,
    *,
    actor: tuple[str, str],
    action: str,
    resource_type: str,
    resource_id: str,
    outcome: str,
    details: Mapping[str, object],
    at: datetime,
) -> None:
    store.record_standalone(
        actor_subject=actor[0],
        actor_issuer=actor[1],
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        details=details,
    )
    store.times = [*(store.times or []), at]


@pytest.fixture(name="audit_store", autouse=True)
def _audit_store_fixture(monkeypatch: pytest.MonkeyPatch) -> _TimedAuditStore:  # pyright: ignore[reportUnusedFunction]  # autouse + injected by name
    store = _TimedAuditStore()
    _override(monkeypatch, audit_store_factory=audit_store_factory(store))
    _override(monkeypatch, role_store_factory=_fake_store_factory(_seeded_store()))
    _write(
        store,
        actor=_DENIED,
        action="access.gate",
        resource_type="operation",
        resource_id="set-catalog-source",
        outcome="rejected",
        details={"required_role": "SystemAdmin"},
        at=_EARLY,
    )
    _write(
        store,
        actor=_ENROLLER,
        action="passkey.enroll",
        resource_type="pending_approval",
        resource_id="approval-1",
        outcome="applied",
        details={"proof": "approval_code"},
        at=_LATE,
    )
    _write(
        store,
        actor=_QUERIER,
        action=QUERY_CYPHER_ACTION,
        resource_type="query",
        resource_id="cypher",
        outcome="applied",
        details={"row_count": 3, "truncated": False},
        at=_LATE,
    )
    return store


def _call(**kwargs: object) -> CallToolResult:
    configure()
    with _verified_actor(sub=_SYSTEM_OWNER_SUBJECT):
        result = asyncio.run(_server().call_tool("list-audit-events", kwargs))
    assert isinstance(result, CallToolResult)
    return result


def _events(result: CallToolResult) -> list[dict[str, object]]:
    assert result.is_error is False
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert not block.text.startswith("error: "), block.text
    events: list[dict[str, object]] = json.loads(block.text)["events"]
    return events


def _actions(result: CallToolResult) -> list[object]:
    return [event["action"] for event in _events(result)]


@pytest.mark.parametrize(
    ("actor", "action"),
    [(_DENIED, "access.gate"), (_ENROLLER, "passkey.enroll"), (_QUERIER, "query.cypher")],
)
def test_new_rows_are_returned_when_filtering_by_actor(actor: tuple[str, str], action: str) -> None:
    result = _call(actor_subject=actor[0])

    assert _actions(result) == [action]


@pytest.mark.parametrize("action", ["access.gate", "passkey.enroll", "query.cypher"])
def test_each_new_action_is_returned_when_filtering_by_that_action(action: str) -> None:
    result = _call(action=action)

    assert _actions(result) == [action]


def test_new_rows_are_returned_when_filtering_by_time_range() -> None:
    late = _call(occurred_from="2026-06-01T00:00:00+00:00")
    early = _call(occurred_to="2026-06-01T00:00:00+00:00")

    assert sorted(str(a) for a in _actions(late)) == ["passkey.enroll", "query.cypher"]
    assert _actions(early) == ["access.gate"]


@pytest.mark.parametrize(
    ("resource_type", "action"),
    [
        ("operation", "access.gate"),
        ("pending_approval", "passkey.enroll"),
        ("query", "query.cypher"),
    ],
)
def test_filtering_by_a_new_resource_type_is_accepted(resource_type: str, action: str) -> None:
    result = _call(resource_type=resource_type)

    assert _actions(result) == [action]
