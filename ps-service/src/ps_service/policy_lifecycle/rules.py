"""ABAC rules that gate the Policy proposal lifecycle (propose, approve, reject, revert, edit).

`PolicyLifecycleRuleContext` carries the facts of one lifecycle call, and the three rule functions
(`require_status`, `require_owner`, `block_self_approval`) each turn it into an `AccessRuleResult`.
They live here, not in `ps_service.authz.rules`, because `ps_service.authz` owns only the generic
`AccessRule`/`AccessRuleResult` machinery and its own grant/revoke rules; this component supplies
its domain-specific rules on top of that shared machinery.

The context's `action` includes `"read"` so a read-visibility check can build a context for
`require_owner` without a value that fits no real action; `require_status` and
`block_self_approval` are never called with `"read"`. It also includes `"edit"`, the action of the
draft-content edit tools, which `require_status` maps to the required status `"draft"`.
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

    Attributes:
        actor: The caller as a `(subject, issuer)` pair, the convention of
            `ps_service.authz.rules.AccessRuleContext.actor`; the same subject under a different
            issuer is a different person.
        owner: The Policy's owner as a `(subject, issuer)` pair, compared whole against `actor`.
        action: The lifecycle action being attempted; `"read"` is used only for visibility checks.
        current_status: The Policy's current status.
    """

    actor: tuple[str, str]
    owner: tuple[str, str]
    action: Literal["propose", "approve", "reject", "revert", "read", "edit"]
    current_status: Literal["draft", "proposed", "approved", "deprecated"]


def require_status(ctx: PolicyLifecycleRuleContext) -> AccessRuleResult:
    """Allow an action only when the Policy is in the status that action requires.

    Business Rules: `propose` and `edit` require `draft`; `approve`, `reject` and `revert` require
    `proposed`. `deprecated` and `approved` are never any action's required status, so they always
    fail; an action with no required status (`read`) also fails.

    Args:
        ctx: The lifecycle call's facts; `ctx.action` selects the required status and
            `ctx.current_status` is compared against it.

    Returns:
        `AccessRuleResult(allowed=False, reason="cannot <action> a Policy in status '<status>'")`
        when `ctx.current_status` differs from the required status, else
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
    """Allow an action only when the caller is the Policy's owner.

    Used for `propose`, `revert`, the draft edits and read visibility of a draft.

    Args:
        ctx: The lifecycle call's facts; `ctx.actor` and `ctx.owner` are compared as the full
            `(subject, issuer)` pair.

    Returns:
        `AccessRuleResult(allowed=True, reason=None)` when `ctx.actor == ctx.owner`, else
        `AccessRuleResult(allowed=False, reason="only the Policy's owner may do this")`.
    """
    if ctx.actor == ctx.owner:
        return AccessRuleResult(allowed=True, reason=None)
    return AccessRuleResult(allowed=False, reason="only the Policy's owner may do this")


def block_self_approval(ctx: PolicyLifecycleRuleContext) -> AccessRuleResult:
    """Refuse a caller who tries to approve or reject a Policy they own.

    Business Rules: the comparison is on the whole `(subject, issuer)` pair, so the same subject
    under a different issuer is a different person and is not blocked.

    Args:
        ctx: The lifecycle call's facts; `ctx.actor` and `ctx.owner` are compared as the full
            `(subject, issuer)` pair.

    Returns:
        `AccessRuleResult(allowed=False, reason="you cannot approve or reject a Policy you own")`
        when `ctx.actor == ctx.owner`, else `AccessRuleResult(allowed=True, reason=None)`.
    """
    if ctx.actor == ctx.owner:
        return AccessRuleResult(
            allowed=False, reason="you cannot approve or reject a Policy you own"
        )
    return AccessRuleResult(allowed=True, reason=None)
