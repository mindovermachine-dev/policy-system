"""The Policy proposal-lifecycle ABAC rules (issue #134, PLAN.md D-2/D-3).

`PolicyLifecycleRuleContext` and its three rules live here, not in
`ps_service.authz.rules` -- `ps_service.authz` owns only the generic
`AccessRule`/`AccessRuleResult` machinery and its own two grant/revoke rules
(untouched by this issue). This mirrors the existing precedent of
independently-owned components vendoring their own domain-specific pieces on
top of shared infrastructure.

`action`'s Literal is the CHANGES.md-widened version (finding 6, Appendix A):
it includes `"read"` alongside PLAN.md's original
`"propose"/"approve"/"reject"/"revert"`, so `ps_service.policy_lifecycle
.service.get_policy` (a later slice) can construct a context for its
`require_owner` visibility check without a value that fits no real action.
Neither `require_status` nor `block_self_approval` is ever called with
`action="read"` -- `require_status`'s per-action status lookup only ever
receives one of the four transition actions.

Issue #136 (Slice 1, PLAN.md §1.2) widens `action` a second time, adding
`"edit"` -- the six draft-content PATCH/add tools' own status-gate action,
paired with `_REQUIRED_STATUS_BY_ACTION["edit"] = "draft"` below. Every one
of those six tools builds a `PolicyLifecycleRuleContext` with
`action="edit"` for both `require_owner` (owner-or-elevated-role gate) and
`require_status` (must currently be `"draft"`) -- `require_owner` needed no
change at all (it is already action-agnostic); `require_status` picks up
the new action for free via `_REQUIRED_STATUS_BY_ACTION`'s own dict lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ps_service.authz.rules import AccessRuleResult

if TYPE_CHECKING:
    from typing import Literal

_REQUIRED_STATUS_BY_ACTION = {
    "propose": "draft",
    "approve": "proposed",
    "reject": "proposed",
    "revert": "proposed",
    "edit": "draft",
}


@dataclass(frozen=True, slots=True)
class PolicyLifecycleRuleContext:
    """The facts one policy-lifecycle `AccessRule` needs to decide an action.

    `actor`/`owner` are `(subject, issuer)` pairs, matching
    `ps_service.authz.rules.AccessRuleContext.actor`/`.target`'s own
    convention -- same subject with a different issuer is a different person
    (AC-BI-017).
    """

    actor: tuple[str, str]
    owner: tuple[str, str]
    action: Literal["propose", "approve", "reject", "revert", "read", "edit"]
    current_status: Literal["draft", "proposed", "approved", "deprecated"]


def require_status(ctx: PolicyLifecycleRuleContext) -> AccessRuleResult:
    """AC-BI-023: an action may only proceed from its own required status.

    `propose` requires `draft`; `approve`/`reject`/`revert` each require
    `proposed`. `deprecated` is never any action's required status, so it
    always fails this check -- deprecated's terminal nature needs no separate
    special case.

    Args:
        ctx: The lifecycle call's facts.

    Returns:
        `AccessRuleResult(allowed=False, ...)` when `ctx.current_status`
        does not match the status required for `ctx.action`, else
        `AccessRuleResult(allowed=True, reason=None)`.
    """
    required_status = _REQUIRED_STATUS_BY_ACTION.get(ctx.action)
    if required_status is None or ctx.current_status != required_status:
        return AccessRuleResult(
            allowed=False,
            reason=f"cannot {ctx.action} a Policy in status '{ctx.current_status}'",
        )
    return AccessRuleResult(allowed=True, reason=None)


def require_owner(ctx: PolicyLifecycleRuleContext) -> AccessRuleResult:
    """AC-BI-003/AC-BI-007: only the Policy's owner may `propose`/`revert` it.

    Args:
        ctx: The lifecycle call's facts.

    Returns:
        `AccessRuleResult(allowed=True, reason=None)` when `ctx.actor ==
        ctx.owner` (compared as the full `(sub, iss)` pair), else
        `AccessRuleResult(allowed=False, ...)`.
    """
    if ctx.actor == ctx.owner:
        return AccessRuleResult(allowed=True, reason=None)
    return AccessRuleResult(allowed=False, reason="only the Policy's owner may do this")


def block_self_approval(ctx: PolicyLifecycleRuleContext) -> AccessRuleResult:
    """AC-BI-006: a caller may never approve or reject a Policy they own.

    AC-BI-017 falls out for free from tuple equality: the same subject under
    a different issuer is a different person, so it is not blocked here.

    Args:
        ctx: The lifecycle call's facts.

    Returns:
        `AccessRuleResult(allowed=False, ...)` when `ctx.actor == ctx.owner`,
        else `AccessRuleResult(allowed=True, reason=None)`.
    """
    if ctx.actor == ctx.owner:
        return AccessRuleResult(
            allowed=False, reason="you cannot approve or reject a Policy you own"
        )
    return AccessRuleResult(allowed=True, reason=None)
