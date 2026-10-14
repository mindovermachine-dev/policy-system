"""The `AccessRule` (ABAC) extension point and the grant/revoke rules built on it.

`AccessRule` is a plain typed callable, not a persisted or configurable rule engine: there
is no rule table and no dynamic registration. `ps_service.authz.service.grant_role` and
`revoke_role` each evaluate a fixed, local sequence of rules after the role-based check
passes and before the store mutation runs.

The rules defined here are `block_self_target` (a caller may not grant or revoke their own
roles, with a `SystemOwner` exemption) and `enforce_system_owner_floor` (the last active
`SystemOwner` may not be revoked). `revoke_role` evaluates `block_self_target` first and
`enforce_system_owner_floor` only for a `SYSTEM_OWNER` target, so the self-block wins when
both apply, for example the sole `SystemOwner` revoking their own role.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ps_service.authz.models import AccessRole

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal


@dataclass(frozen=True, slots=True)
class AccessRuleContext:
    """The facts one `AccessRule` needs to decide whether a grant or revoke may proceed.

    Attributes:
        actor: The calling principal's verified `(sub, iss)` identity.
        target: The `(sub, iss)` identity of the principal the role is granted to or
            revoked from.
        access_role: The role being granted or revoked.
        action: `"grant"` or `"revoke"`.
        active_system_owner_count: The number of active `SystemOwner`s before the action
            takes effect. Only `enforce_system_owner_floor` reads it, and it is meaningful
            only when `access_role` is `SYSTEM_OWNER`; callers evaluating other rules pass 0.
        actor_is_system_owner: Whether the actor holds `SYSTEM_OWNER`. Only
            `block_self_target`'s exemption reads it. Defaults to `False`.
    """

    actor: tuple[str, str]
    target: tuple[str, str]
    access_role: AccessRole
    action: Literal["grant", "revoke"]
    active_system_owner_count: int
    actor_is_system_owner: bool = False


@dataclass(frozen=True, slots=True)
class AccessRuleResult:
    """One `AccessRule`'s verdict on whether an action may proceed.

    Attributes:
        allowed: `True` when the action may proceed, `False` when the rule blocks it.
        reason: A human-readable explanation of the block, or `None` when `allowed` is
            `True`.
    """

    allowed: bool
    reason: str | None


type AccessRule[CtxT] = Callable[[CtxT], AccessRuleResult]
"""One ABAC rule function: given the call's facts, decide whether it may proceed.

A rule takes one context object of type `CtxT` and returns an `AccessRuleResult`. It is
generic over the context type so that components other than `ps_service.authz` can define
their own rule-context shape (for example
`ps_service.policy_lifecycle.rules.PolicyLifecycleRuleContext`) and still be typed as
`AccessRule[TheirContext]`. The rules in this module satisfy `AccessRule[AccessRuleContext]`.
"""


def block_self_target(ctx: AccessRuleContext) -> AccessRuleResult:
    """Block a caller from granting or revoking their own access roles.

    Prevents a principal from escalating or removing their own access. A `SystemOwner` is
    exempt, so a single principal can run an evaluation end to end: they may grant
    themselves any role and revoke any role from themselves except `SYSTEM_OWNER`.
    Self-revoking `SYSTEM_OWNER` stays blocked even for a `SystemOwner`.

    Args:
        ctx: The grant or revoke call's facts. `ctx.actor` and `ctx.target` are compared as
            the full `(sub, iss)` pair; `ctx.actor_is_system_owner` selects the exemption;
            `ctx.active_system_owner_count` is ignored.

    Returns:
        `AccessRuleResult(allowed=False, reason=...)` when `ctx.actor == ctx.target` and the
        exemption does not apply, otherwise `AccessRuleResult(allowed=True, reason=None)`.

    Business Rules:
        A caller may not grant or revoke their own access roles, except that a
        `SystemOwner` may self-grant any role and self-revoke any role other than
        `SYSTEM_OWNER`.
    """
    if ctx.actor != ctx.target:
        return AccessRuleResult(allowed=True, reason=None)
    if ctx.actor_is_system_owner and not (
        ctx.action == "revoke" and ctx.access_role is AccessRole.SYSTEM_OWNER
    ):
        return AccessRuleResult(allowed=True, reason=None)
    return AccessRuleResult(
        allowed=False, reason="a caller may not grant or revoke their own access roles"
    )


def enforce_system_owner_floor(ctx: AccessRuleContext) -> AccessRuleResult:
    """Block a revoke that would remove the last remaining active `SystemOwner`.

    Keeps the system administrable: at least one `SystemOwner` must always remain. The rule
    is only meaningful for a `SYSTEM_OWNER` revoke; `revoke_role` builds a context for it
    only in that case.

    Args:
        ctx: The revoke call's facts, with `ctx.access_role == SYSTEM_OWNER`.
            `ctx.active_system_owner_count` is the number of active `SystemOwner`s before
            this revoke would take effect. When it is one, the target is that remaining
            owner, so deleting their row would leave zero.

    Returns:
        `AccessRuleResult(allowed=False, reason=...)` when `ctx.active_system_owner_count
        <= 1`, otherwise `AccessRuleResult(allowed=True, reason=None)`.

    Business Rules:
        The number of active `SystemOwner`s may never drop to zero through a revoke.
    """
    if ctx.active_system_owner_count <= 1:
        return AccessRuleResult(
            allowed=False, reason="this action would leave zero active SystemOwners"
        )
    return AccessRuleResult(allowed=True, reason=None)
