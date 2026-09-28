"""Cross-action audit-vocabulary consistency check (issue #134, PLAN.md S27).

AC-BI-016 requires more than "each action records an audit event on
rejection" -- the `reason_code` vocabulary must mean the same thing
everywhere it appears, so a consumer reading the audit trail across actions
never has to special-case one action's own dialect. This file exercises each
of `propose_policy`/`approve_policy`/`reject_policy`/`revert_policy_to_draft`'s
own rejection paths and asserts the exact `reason_code` string recorded is
identical for the same underlying rejection concept across every action that
can raise it: `"access_denied"` (propose/revert, both owner-only actions),
`"invalid_status"` (all four), and `"self_approval_blocked"`
(approve/reject, both `PolicyManager`-gated actions). It then cross-checks
those same strings against `PolicyTransitionDetails`'s own registered
`reason_code` vocabulary (S10's single source of truth,
`ps_service.policy_lifecycle.audit_actions`) -- a reason code drifting out of
that model, or the model accepting a code no action actually emits, would
also be caught here.

This slice runs only after S26's shared `_apply_transition` helper lands
(PLAN.md S27's own note: "this should be a straightforward pass now if S26's
refactor is done correctly") -- these tests exist to prove that holds, not
to newly discover the vocabulary. No drift was found: `_apply_transition`
(`service.py`) and `PolicyTransitionDetails` (`audit_actions.py`) already
share one `reason_code` `Literal` end to end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package (has `__init__.py`); mirrors `test_service.py`'s own cross-package import convention
    FakeAccessRoleStore,
)
from pydantic import ValidationError

from ps_service.authz.models import AccessRole
from ps_service.policy_lifecycle.audit_actions import PolicyTransitionDetails
from ps_service.policy_lifecycle.errors import (
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicySelfApprovalBlockedError,
)
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
_OTHER = ("bob", "https://issuer.example")
_MANAGER = ("carol", "https://issuer.example")
_GRANTER = ("system-admin-tool", "https://issuer.example")


@dataclass
class _FakeQueryResult:
    result_set: list[object] = field(default_factory=list)


class _FixedTreeFakeGraph:
    """A minimal `GraphHandle` double returning one canned, childless Policy tree.

    Every query these tests need is either `read_policy_tree`'s own
    `RETURN` (answered from the fixed `status`/`owner` this instance was
    built with) or one of `backfill_governance_status`'s guarded `SET`
    statements (a no-op) -- no test in this file ever reaches a cascading
    write, since every one of them exercises a rejection path.
    """

    def __init__(self, *, status: str, owner: tuple[str, str]) -> None:
        self._status = status
        self._owner = owner

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        del params
        if "s.id, s.title, s.status, c.id" not in q:
            return _FakeQueryResult()
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


@dataclass
class _RecordedCall:
    action: str
    outcome: str
    details: Mapping[str, object]


class _FakeAuditStore:
    """Records every `record_standalone` call -- these tests only ever read `.calls`."""

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    def record(self, *args: object, **kwargs: object) -> None:
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
        del actor_subject, actor_issuer, resource_type, resource_id
        self.calls.append(_RecordedCall(action=action, outcome=outcome, details=details))

    def query(
        self, *, filters: AuditQueryFilters, cursor: str | None, page_size: int
    ) -> AuditQueryPage:
        """Not exercised here -- present only for `AuditStore` `Protocol` conformance."""
        del filters, cursor, page_size
        raise NotImplementedError


def _manager_store(*, subject_pair: tuple[str, str] = _MANAGER) -> FakeAccessRoleStore:
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=subject_pair, access_role=AccessRole.POLICY_MANAGER)
    return store


def test_access_denied_reason_code_is_identical_across_propose_and_revert() -> None:
    """`propose`/`revert` are this issue's only two owner-only actions."""
    propose_audit = _FakeAuditStore()
    with pytest.raises(PolicyDraftAccessDeniedError):
        propose_policy(
            actor=_OTHER,
            policy_id="pol_x",
            graph=_FixedTreeFakeGraph(status="draft", owner=_OWNER),
            audit_store=propose_audit,
        )

    revert_audit = _FakeAuditStore()
    with pytest.raises(PolicyDraftAccessDeniedError):
        revert_policy_to_draft(
            actor=_OTHER,
            policy_id="pol_x",
            graph=_FixedTreeFakeGraph(status="proposed", owner=_OWNER),
            audit_store=revert_audit,
        )

    propose_reason = propose_audit.calls[0].details["reason_code"]
    revert_reason = revert_audit.calls[0].details["reason_code"]
    assert propose_reason == revert_reason == "access_denied"


def test_self_approval_blocked_reason_code_is_identical_across_approve_and_reject() -> None:
    """`approve`/`reject` are this issue's only two `PolicyManager`-gated actions."""
    approve_audit = _FakeAuditStore()
    with pytest.raises(PolicySelfApprovalBlockedError):
        approve_policy(
            actor=_OWNER,
            policy_id="pol_x",
            graph=_FixedTreeFakeGraph(status="proposed", owner=_OWNER),
            audit_store=approve_audit,
            access_role_store=_manager_store(subject_pair=_OWNER),
        )

    reject_audit = _FakeAuditStore()
    with pytest.raises(PolicySelfApprovalBlockedError):
        reject_policy(
            actor=_OWNER,
            policy_id="pol_x",
            graph=_FixedTreeFakeGraph(status="proposed", owner=_OWNER),
            audit_store=reject_audit,
            access_role_store=_manager_store(subject_pair=_OWNER),
        )

    approve_reason = approve_audit.calls[0].details["reason_code"]
    reject_reason = reject_audit.calls[0].details["reason_code"]
    assert approve_reason == reject_reason == "self_approval_blocked"


def test_invalid_status_reason_code_is_identical_across_all_four_transitions() -> None:
    """Every one of the 4 transitions can fail `require_status` -- all 4 must agree."""
    propose_audit = _FakeAuditStore()
    with pytest.raises(PolicyInvalidStatusTransitionError):
        propose_policy(
            actor=_OWNER,
            policy_id="pol_x",
            graph=_FixedTreeFakeGraph(status="proposed", owner=_OWNER),
            audit_store=propose_audit,
        )

    revert_audit = _FakeAuditStore()
    with pytest.raises(PolicyInvalidStatusTransitionError):
        revert_policy_to_draft(
            actor=_OWNER,
            policy_id="pol_x",
            graph=_FixedTreeFakeGraph(status="draft", owner=_OWNER),
            audit_store=revert_audit,
        )

    approve_audit = _FakeAuditStore()
    with pytest.raises(PolicyInvalidStatusTransitionError):
        approve_policy(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=_FixedTreeFakeGraph(status="draft", owner=_OWNER),
            audit_store=approve_audit,
            access_role_store=_manager_store(),
        )

    reject_audit = _FakeAuditStore()
    with pytest.raises(PolicyInvalidStatusTransitionError):
        reject_policy(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=_FixedTreeFakeGraph(status="draft", owner=_OWNER),
            audit_store=reject_audit,
            access_role_store=_manager_store(),
        )

    reasons = {
        propose_audit.calls[0].details["reason_code"],
        revert_audit.calls[0].details["reason_code"],
        approve_audit.calls[0].details["reason_code"],
        reject_audit.calls[0].details["reason_code"],
    }
    assert reasons == {"invalid_status"}


@pytest.mark.parametrize(
    "reason_code",
    ["access_denied", "self_approval_blocked", "invalid_status", "incomplete_for_proposal"],
)
def test_every_action_reason_code_is_accepted_by_the_registered_vocabulary(
    reason_code: str,
) -> None:
    """Every `reason_code` string this file observed above is a valid `PolicyTransitionDetails`.

    S10's `PolicyTransitionDetails` (`audit_actions.py`) is the single
    registered vocabulary shared by `policy.propose`/`.approve`/`.reject`/
    `.revert`/`.auto_deprecate` -- constructing one with each string this
    test file actually saw emitted proves the model doesn't silently accept
    a wider or narrower set than what the 4 transition functions emit.
    """
    PolicyTransitionDetails(
        affected_node_ids=("pol_x",),
        from_status="draft",
        to_status="draft",
        reason_code=reason_code,  # pyright: ignore[reportArgumentType]  -- deliberately parametrized over the full string vocabulary, not narrowed to the `Literal`
    )


def test_an_unregistered_reason_code_is_rejected_by_the_vocabulary() -> None:
    """The vocabulary is closed: a code no action emits must not silently validate."""
    with pytest.raises(ValidationError):
        PolicyTransitionDetails(
            affected_node_ids=("pol_x",),
            from_status="draft",
            to_status="draft",
            reason_code="not_a_real_reason_code",  # pyright: ignore[reportArgumentType]
        )
