"""Immutability matrix test (issue #134, PLAN.md S23, AC-BI-011).

AC-BI-011 requires "no tool mutates the content of an approved or deprecated
tree." Within #134's own scope, no dedicated content-mutation tool exists in
this codebase yet -- content editing (renaming a Policy, adding/removing a
Standard/Control, changing a Control's `control_type`, etc.) is entirely
#136's scope. What #134 DOES own is the 4 status-transition entry points
(`propose_policy`/`approve_policy`/`reject_policy`/`revert_policy_to_draft`),
and this test is the concrete, within-#134-scope verification that every one
of them refuses to run its own cascading write from the two statuses it does
not apply from beyond the one it does (`"approved"`/`"deprecated"` -- see
`ps_service.policy_lifecycle.rules.require_status`'s own
`_REQUIRED_STATUS_BY_ACTION` table, D-3): the call is rejected by
`PolicyInvalidStatusTransitionError` before `graph_writer.cascade_status`'s
own cascading `SET` is ever issued -- a parametrized, no-mutation regression
guard, not new production code.

Forward pointer for #136: when that issue adds its own content-editing
tool(s) (a Policy rename, a Standard/Control add/remove/edit), each one must
independently apply the same `require_status`-shaped precondition (reject
whenever the target tree's root Policy is `"approved"`/`"deprecated"`) --
this test file's own matrix does not, and cannot, cover a tool that does not
exist yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package (has `__init__.py`); mirrors `test_service.py`'s own cross-package import convention
    FakeAccessRoleStore,
)

from ps_service.authz.models import AccessRole
from ps_service.policy_lifecycle.errors import PolicyInvalidStatusTransitionError
from ps_service.policy_lifecycle.service import (
    approve_policy,
    propose_policy,
    reject_policy,
    revert_policy_to_draft,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Literal

    from ps_service.audit.models import AuditQueryFilters, AuditQueryPage

_OWNER = ("alice", "https://issuer.example")
_MANAGER = ("carol", "https://issuer.example")
_GRANTER = ("system-admin-tool", "https://issuer.example")


@dataclass
class _FakeQueryResult:
    result_set: list[object] = field(default_factory=list)


class _ImmutableTreeFakeGraph:
    """A `GraphHandle` double for one fixed, childless Policy tree at a given status.

    Answers `graph_writer.read_policy_tree`'s single `RETURN` query with a
    canned row; every other query (each of `backfill_governance_status`'s
    three guarded `SET ... IS NULL` statements) is a no-op. `write_queries`
    only ever records `graph_writer.cascade_status`'s own distinguishing
    `SET p.status = $target_status` statement -- this file's whole point is
    that this list must stay empty for every case in the matrix below.
    """

    def __init__(self, *, status: str, owner: tuple[str, str] = _OWNER) -> None:
        self._status = status
        self._owner = owner
        self.write_queries: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        del params
        if "SET p.status = $target_status" in q:
            self.write_queries.append(q)
            return _FakeQueryResult()
        if "s.id, s.title, s.status, c.id" in q:
            row = [
                "pol_x",
                "Title",
                self._status,
                "1",
                self._owner[0],
                self._owner[1],
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ]
            return _FakeQueryResult(result_set=[row])
        # `backfill_governance_status`'s own three guarded statements: no-op.
        return _FakeQueryResult()


class _FakeAuditStore:
    """Records nothing of interest to this file beyond accepting the call.

    Every case below asserts on `PolicyInvalidStatusTransitionError` and
    `graph.write_queries`, not on the audit trail's own shape (already
    covered per-action in `test_service.py`).
    """

    def record(self, *args: object, **kwargs: object) -> str:
        raise NotImplementedError

    def record_standalone(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: Literal["applied", "rejected", "failed"],
        details: Mapping[str, object],
    ) -> None:
        del actor_subject, actor_issuer, action, resource_type, resource_id, outcome, details

    def query(
        self, *, filters: AuditQueryFilters, cursor: str | None, page_size: int
    ) -> AuditQueryPage:
        """Not exercised here -- present only for `AuditStore` `Protocol` conformance."""
        del filters, cursor, page_size
        raise NotImplementedError


def _manager_store() -> FakeAccessRoleStore:
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)
    return store


def _run_propose(graph: _ImmutableTreeFakeGraph) -> None:
    propose_policy(actor=_OWNER, policy_id="pol_x", graph=graph, audit_store=_FakeAuditStore())


def _run_approve(graph: _ImmutableTreeFakeGraph) -> None:
    approve_policy(
        actor=_MANAGER,
        policy_id="pol_x",
        graph=graph,
        audit_store=_FakeAuditStore(),
        access_role_store=_manager_store(),
    )


def _run_reject(graph: _ImmutableTreeFakeGraph) -> None:
    reject_policy(
        actor=_MANAGER,
        policy_id="pol_x",
        graph=graph,
        audit_store=_FakeAuditStore(),
        access_role_store=_manager_store(),
    )


def _run_revert(graph: _ImmutableTreeFakeGraph) -> None:
    revert_policy_to_draft(
        actor=_OWNER, policy_id="pol_x", graph=graph, audit_store=_FakeAuditStore()
    )


# `approve`/`reject` use a non-owner `_MANAGER` actor (so `block_self_approval`
# passes and the call reaches `require_status`); `propose`/`revert` use the
# owner-only `_OWNER` actor (so `require_owner` passes for the same reason).
_ACTIONS = {
    "propose": _run_propose,
    "approve": _run_approve,
    "reject": _run_reject,
    "revert": _run_revert,
}


@pytest.mark.parametrize("status", ["approved", "deprecated"])
@pytest.mark.parametrize("action", sorted(_ACTIONS))
def test_transition_from_immutable_status_is_rejected_with_no_graph_mutation(
    action: str, status: str
) -> None:
    """AC-BI-011: none of the 4 transitions ever cascades from `approved`/`deprecated`.

    Neither status is any action's own `require_status`-required starting
    point (D-3's `_REQUIRED_STATUS_BY_ACTION` names only `"draft"`/
    `"proposed"`), so every one of these 8 cases is rejected by
    `PolicyInvalidStatusTransitionError` before `graph_writer.cascade_status`
    is ever reached -- proven here by asserting zero cascading writes, not
    just the raised exception type.
    """
    graph = _ImmutableTreeFakeGraph(status=status)

    with pytest.raises(PolicyInvalidStatusTransitionError):
        _ACTIONS[action](graph)

    assert graph.write_queries == []
