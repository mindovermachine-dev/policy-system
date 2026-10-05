# pyright: reportPrivateUsage=false
"""Fail-closed and sanitised failure over HTTP for EVERY cleanup executor (issue #190, slice 17).

AC-BI-022: the audit row cannot be written -> no graph edit. AC-BI-018: a graph write fails ->
no partial change is reported and the error carries no internal detail. The five signing
worlds (capability merge, obligation merge, release, capability unmerge, obligation unmerge)
are driven through the real signing router, executor registry and executors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import pytest
import redis.exceptions

# Each module's own `world` fixture (monkeypatches the executor dependencies for that flow).
from passkey_signing.test_graph_cleanup_obligation_signing_to_merge import (
    world as obligation_merge_world,
)
from passkey_signing.test_graph_cleanup_release_signing import (
    world as release_world,
)
from passkey_signing.test_graph_cleanup_signing_to_merge import (
    world as capability_merge_world,
)
from passkey_signing.test_graph_cleanup_unmerge_obligation_signing import (
    world as obligation_unmerge_world,
)
from passkey_signing.test_graph_cleanup_unmerge_signing import (
    world as capability_unmerge_world,
)
from ps_service.audit.errors import AuditPostgresUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.passkey_signing.models import PendingApprovalRow

__all__ = [  # the imported `world` fixtures are used by name through `flow`
    "capability_merge_world",
    "capability_unmerge_world",
    "obligation_merge_world",
    "obligation_unmerge_world",
    "release_world",
]

_FLOWS = [
    ("capability_merge_world", "capability.merge"),
    ("obligation_merge_world", "obligation.merge"),
    ("release_world", "capability.release_governance"),
    ("capability_unmerge_world", "capability.unmerge"),
    ("obligation_unmerge_world", "obligation.unmerge"),
]
_LEAKS = ("10.1.2.3", "6379", "relation", "refused")


class _World(Protocol):
    graph: object
    audit: _Audit

    def approve(self) -> tuple[PendingApprovalRow, str]: ...

    def sign(self, row: PendingApprovalRow, code: str) -> _Response: ...


class _Audit(Protocol):
    events: list[str]
    raise_on_outcome: dict[str, Exception]
    rows: list[_Row]


class _Row(Protocol):
    outcome: str
    action: str
    details: dict[str, object]


class _Response(Protocol):
    status_code: int
    text: str

    def json(self) -> dict[str, object]: ...


def _fail_graph_write(world: _World, error: Exception) -> None:
    attribute = "release_error" if hasattr(world.graph, "release_error") else "write_error"
    setattr(world.graph, attribute, error)


@pytest.fixture(params=_FLOWS, ids=[flow for flow, _ in _FLOWS])
def flow(request: pytest.FixtureRequest) -> tuple[_World, str]:
    name, action = request.param
    getter: Callable[[str], object] = request.getfixturevalue
    return getter(name), action  # pyright: ignore[reportReturnType]  -- the five worlds share the Protocol shape


def test_audit_outage_means_no_graph_write_and_no_leak(flow: tuple[_World, str]) -> None:
    world, _action = flow
    row, code = world.approve()
    world.audit.raise_on_outcome["applied"] = AuditPostgresUnavailableError(
        "db host 10.1.2.3 down: relation audit_events refused"
    )

    response = world.sign(row, code)

    assert "graph_write" not in world.audit.events
    assert world.audit.rows == []
    assert "error" in response.json()
    assert not any(leak in response.text for leak in _LEAKS)


def test_graph_write_failure_is_audited_failed_with_a_generic_error(
    flow: tuple[_World, str],
) -> None:
    world, action = flow
    row, code = world.approve()
    _fail_graph_write(world, redis.exceptions.ConnectionError("host=10.1.2.3 port=6379 refused"))

    response = world.sign(row, code)

    assert [(r.action, r.outcome) for r in world.audit.rows] == [
        (action, "applied"),
        (action, "failed"),
    ]
    assert world.audit.rows[0].details["approval_id"] == world.audit.rows[1].details["approval_id"]
    body = response.json()
    assert "error" in body
    assert not any(leak in response.text for leak in _LEAKS)
