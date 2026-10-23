"""Immutability matrix test.

No tool may mutate the content of an approved or deprecated tree. Within this
module's scope, no dedicated content-mutation tool exists -- content editing
(renaming a Policy, adding/removing a Standard/Control, changing a Control's
`control_type`, etc.) is outside it. What this module DOES own is the 4 status-transition
entry points
(`propose_policy`/`approve_policy`/`reject_policy`/`revert_policy_to_draft`),
and this test is the concrete verification that every one
of them refuses to run its own cascading write from the two statuses it does
not apply from beyond the one it does (`"approved"`/`"deprecated"` -- see
`ps_service.policy_lifecycle.rules.require_status`'s own
`_REQUIRED_STATUS_BY_ACTION` table): the call is rejected by
`PolicyInvalidStatusTransitionError` before `graph_writer.cascade_status`'s
own cascading group is ever submitted -- a parametrized, no-mutation regression
guard, not new production code.

Forward pointer: when a content-editing
tool is added (a Policy rename, a Standard/Control add/remove/edit), each one must
independently apply the same `require_status`-shaped precondition (reject
whenever the target tree's root Policy is `"approved"`/`"deprecated"`) --
this test file's own matrix does not, and cannot, cover a tool that does not
exist yet.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package (has `__init__.py`); mirrors `test_service.py`'s own cross-package import convention
    FakeAccessRoleStore,
)

from policy_lifecycle._gateway_fakes import POLICY_SYSTEM, policy_manager_enforcer
from ps_service.authz.models import AccessRole
from ps_service.policy_lifecycle.errors import PolicyInvalidStatusTransitionError
from ps_service.policy_lifecycle.service import (
    approve_policy,
    propose_policy,
    reject_policy,
    revert_policy_to_draft,
)

if TYPE_CHECKING:
    from policy_lifecycle._gateway_fakes import LifecycleRig, PolicyReadGraph
    from ps_service.audit.models import AuditQueryFilters, AuditQueryPage

_OWNER = ("alice", "https://issuer.example")
_MANAGER = ("carol", "https://issuer.example")
_GRANTER = ("system-admin-tool", "https://issuer.example")


def _tree(rig: LifecycleRig, *, status: str) -> PolicyReadGraph:
    """One childless Policy `pol_x` at `status`, as the rig graph holds it."""
    rig.seed_policy("pol_x", title="Title", status=status, owner=_OWNER, legacy_edges=False)
    return rig.read


class _AcceptingAuditStore:
    """Accepts every row without validating it: this file asserts on the raised error and the log.

    The registry-validating rig store would refuse a rejected row whose `from_status` is
    `deprecated` (not a value `PolicyTransitionDetails` declares); that is not what is under test.
    """

    def record(self, *args: object, **kwargs: object) -> str:
        raise NotImplementedError

    def record_standalone(self, **kwargs: object) -> None:
        del kwargs

    def record_standalone_with_event_id(self, **kwargs: object) -> str:
        del kwargs
        return str(uuid.uuid4())

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


def _run_propose(rig: LifecycleRig, graph: PolicyReadGraph) -> None:
    propose_policy(
        actor=_OWNER,
        policy_id="pol_x",
        graph=graph,
        audit_store=_AcceptingAuditStore(),
        submitter=rig.gateway,
        graph_name=POLICY_SYSTEM,
    )


def _run_approve(rig: LifecycleRig, graph: PolicyReadGraph) -> None:
    approve_policy(
        actor=_MANAGER,
        policy_id="pol_x",
        graph=graph,
        audit_store=_AcceptingAuditStore(),
        enforce_policy_manager=policy_manager_enforcer(_manager_store()),
        submitter=rig.gateway,
        graph_name=POLICY_SYSTEM,
    )


def _run_reject(rig: LifecycleRig, graph: PolicyReadGraph) -> None:
    reject_policy(
        actor=_MANAGER,
        policy_id="pol_x",
        graph=graph,
        audit_store=_AcceptingAuditStore(),
        enforce_policy_manager=policy_manager_enforcer(_manager_store()),
        submitter=rig.gateway,
        graph_name=POLICY_SYSTEM,
    )


def _run_revert(rig: LifecycleRig, graph: PolicyReadGraph) -> None:
    revert_policy_to_draft(
        actor=_OWNER,
        policy_id="pol_x",
        graph=graph,
        audit_store=_AcceptingAuditStore(),
        submitter=rig.gateway,
        graph_name=POLICY_SYSTEM,
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
    lifecycle_rig: LifecycleRig, action: str, status: str
) -> None:
    """None of the 4 transitions ever cascades from `approved`/`deprecated`.

    Neither status is any action's own `require_status`-required starting
    point (`_REQUIRED_STATUS_BY_ACTION` names only `"draft"`/
    `"proposed"`), so every one of these 8 cases is rejected by
    `PolicyInvalidStatusTransitionError` before `graph_writer.cascade_status`
    is ever reached -- proven here by asserting zero cascading writes, not
    just the raised exception type.
    """
    graph = _tree(lifecycle_rig, status=status)
    position = lifecycle_rig.store.last_position("policy_system")

    with pytest.raises(PolicyInvalidStatusTransitionError):
        _ACTIONS[action](lifecycle_rig, graph)

    assert lifecycle_rig.store.last_position("policy_system") == position
    assert lifecycle_rig.store.groups == []
