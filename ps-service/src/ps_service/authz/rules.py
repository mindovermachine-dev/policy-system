"""The `AccessRule` (ABAC) extension point (issue #133, PLAN.md §0.13).

`AccessRule` is a plain typed callable, not a persisted/configurable rule
engine (no rule table, no dynamic registration API -- deliberately, YAGNI).
`ps_service.authz.service`'s `grant_role`/`revoke_role` each evaluate a
fixed, local tuple of these after the RBAC check passes and before the
store mutation runs.

This slice adds `enforce_system_owner_floor` (AC-BI-006), alongside the
`block_self_target` (AC-BI-005) rule shipped in Slice 2 -- `revoke_role` now
accepts `SYSTEM_OWNER` as a target (CHANGES.md Appendix A), and
`enforce_system_owner_floor` is `revoke_role`'s second rule, evaluated only
for that target, and only *after* `block_self_target` has already passed
(PLAN.md §4 Slice 3: self-block must win when both conditions would
otherwise apply, e.g. the sole SystemOwner revoking their own role).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal

    from ps_service.authz.models import AccessRole


@dataclass(frozen=True, slots=True)
class AccessRuleContext:
    """The facts one `AccessRule` needs to decide whether a grant/revoke may proceed.

    `active_system_owner_count` is only meaningful when `access_role` is
    `SYSTEM_OWNER` (consumed by Slice 3's `enforce_system_owner_floor`) --
    `block_self_target` never reads it.
    """

    actor: tuple[str, str]
    target: tuple[str, str]
    access_role: AccessRole
    action: Literal["grant", "revoke"]
    active_system_owner_count: int


@dataclass(frozen=True, slots=True)
class AccessRuleResult:
    """One `AccessRule`'s verdict: whether the action may proceed, and why not."""

    allowed: bool
    reason: str | None


type AccessRule[CtxT] = Callable[[CtxT], AccessRuleResult]
"""One ABAC rule function: given the call's facts, decide whether it may proceed.

Generic over the context type (issue #134 deliverable #7) so components other
than `ps_service.authz` can define their own rule-context shape (e.g.
`ps_service.policy_lifecycle.rules.PolicyLifecycleRuleContext`) and still get
`AccessRule[TheirContext]` typing -- a non-breaking widening: every existing
`AccessRule` value here still satisfies `AccessRule[AccessRuleContext]`
unchanged.
"""


def block_self_target(ctx: AccessRuleContext) -> AccessRuleResult:
    """AC-BI-005: a caller may never grant or revoke their own access roles.

    Args:
        ctx: The grant/revoke call's facts.

    Returns:
        `AccessRuleResult(allowed=False, ...)` when `ctx.actor == ctx.target`
        (compared as the full `(sub, iss)` pair), else
        `AccessRuleResult(allowed=True, reason=None)`.
    """
    if ctx.actor == ctx.target:
        return AccessRuleResult(
            allowed=False, reason="a caller may not grant or revoke their own access roles"
        )
    return AccessRuleResult(allowed=True, reason=None)


def enforce_system_owner_floor(ctx: AccessRuleContext) -> AccessRuleResult:
    """AC-BI-006: revoking the last remaining active `SystemOwner` is blocked.

    Only meaningful when `ctx.access_role` is `SYSTEM_OWNER` -- callers other
    than `revoke_role`'s own `SYSTEM_OWNER` branch never construct a context
    that reaches this rule. `ctx.active_system_owner_count` is the count of
    active `SystemOwner`s *before* this revoke would take effect (read by
    `service.py` immediately before evaluating this rule); revoking is
    blocked whenever that count is exactly one -- the target is, by
    construction, that one remaining `SystemOwner`, so deleting their row
    would leave zero (PLAN.md §0.9/CHANGES.md Appendix A step 5).

    Args:
        ctx: The revoke call's facts, `access_role == SYSTEM_OWNER`.

    Returns:
        `AccessRuleResult(allowed=False, ...)` when `ctx.active_system_owner_count
        <= 1`, else `AccessRuleResult(allowed=True, reason=None)`.
    """
    if ctx.active_system_owner_count <= 1:
        return AccessRuleResult(
            allowed=False, reason="this action would leave zero active SystemOwners"
        )
    return AccessRuleResult(allowed=True, reason=None)
