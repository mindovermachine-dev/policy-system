"""Tests for `ps_service.policy_lifecycle.rules` (issue #134, PLAN.md D-3 / S3).

`PolicyLifecycleRuleContext.action` is the CHANGES.md-widened Literal
(finding 6, Appendix A) -- `"read"` is included alongside
`"propose"/"approve"/"reject"/"revert"` even though none of these tests
exercise `"read"` themselves (S13, out of this slice's scope, is its only
consumer).
"""

from __future__ import annotations

from ps_service.policy_lifecycle.rules import (
    PolicyLifecycleRuleContext,
    block_self_approval,
    require_owner,
    require_status,
)

_ACTOR = ("alice", "https://issuer.example")
_OWNER_SAME_ISSUER = ("alice", "https://issuer.example")
_OTHER_SUBJECT = ("bob", "https://issuer.example")
_SAME_SUBJECT_OTHER_ISSUER = ("alice", "https://other-issuer.example")

# --- require_status --------------------------------------------------------


def test_require_status_allows_propose_on_draft() -> None:
    ctx = PolicyLifecycleRuleContext(
        actor=_ACTOR, owner=_ACTOR, action="propose", current_status="draft"
    )
    assert require_status(ctx).allowed


def test_require_status_rejects_approve_on_draft() -> None:
    ctx = PolicyLifecycleRuleContext(
        actor=_ACTOR, owner=_ACTOR, action="approve", current_status="draft"
    )
    result = require_status(ctx)
    assert not result.allowed


def test_require_status_rejects_every_action_on_deprecated() -> None:
    for action in ("propose", "approve", "reject", "revert"):
        ctx = PolicyLifecycleRuleContext(
            actor=_ACTOR, owner=_ACTOR, action=action, current_status="deprecated"
        )
        assert not require_status(ctx).allowed


# --- require_owner ----------------------------------------------------------


def test_require_owner_allows_when_actor_equals_owner() -> None:
    ctx = PolicyLifecycleRuleContext(
        actor=_ACTOR, owner=_OWNER_SAME_ISSUER, action="propose", current_status="draft"
    )
    assert require_owner(ctx).allowed


def test_require_owner_rejects_differing_subject() -> None:
    ctx = PolicyLifecycleRuleContext(
        actor=_ACTOR, owner=_OTHER_SUBJECT, action="propose", current_status="draft"
    )
    assert not require_owner(ctx).allowed


def test_require_owner_rejects_matching_subject_differing_issuer() -> None:
    ctx = PolicyLifecycleRuleContext(
        actor=_ACTOR,
        owner=_SAME_SUBJECT_OTHER_ISSUER,
        action="propose",
        current_status="draft",
    )
    assert not require_owner(ctx).allowed


# --- block_self_approval -----------------------------------------------------


def test_block_self_approval_rejects_when_actor_equals_owner() -> None:
    ctx = PolicyLifecycleRuleContext(
        actor=_ACTOR, owner=_OWNER_SAME_ISSUER, action="approve", current_status="proposed"
    )
    assert not block_self_approval(ctx).allowed


def test_block_self_approval_allows_same_subject_differing_issuer() -> None:
    """AC-BI-017 regression case: same subject, different issuer are different people."""
    ctx = PolicyLifecycleRuleContext(
        actor=_ACTOR,
        owner=_SAME_SUBJECT_OTHER_ISSUER,
        action="approve",
        current_status="proposed",
    )
    assert block_self_approval(ctx).allowed
