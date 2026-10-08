"""Typed `details` for the `user.invite` audit action (issue #195).

Registered with `ps_service.audit` at import time (the idiom of `graph_cleanup.audit_actions`);
the package `__init__` imports this module for that side effect.

The invitee email is the only identifier recorded (D-2: no user id exists at invite time, and the
`itoken` is the redemption token). The invite carries no role or group (D-D), so none is recorded.
The model has no field that could hold an `itoken`, invite URL, credential or free-text error
(AC-BI-009/010); unknown fields are rejected on write by the `AuditDetails` base.
"""

from __future__ import annotations

from ps_service.audit import AuditDetails, register_audit_action, register_audit_resource_type
from ps_service.invitations.errors import (  # noqa: TC001 -- pydantic resolves the annotation at runtime
    InvitationFailureReason,
)

USER_INVITE_ACTION = "user.invite"
USER_RESOURCE_TYPE = "user"


class UserInviteDetails(AuditDetails):
    """`user.invite`: `applied` before the Authentik call; `failed` after it fails."""

    invitee_email: str
    reason_code: InvitationFailureReason | None = None


register_audit_action(USER_INVITE_ACTION, UserInviteDetails)
register_audit_resource_type(USER_RESOURCE_TYPE)
