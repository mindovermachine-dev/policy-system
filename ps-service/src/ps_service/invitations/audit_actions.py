"""Typed `details` for the `user.invite` audit action.

Registered with `ps_service.audit` at import time; the package `__init__` imports this module for
that side effect.

The invitee email is the only identifier recorded: no user id exists at invite time, and the
`itoken` is the redemption token. The invite carries no role or group, so none is recorded. The
model has no field that could hold an `itoken`, invite URL, credential or free-text error;
unknown fields are rejected on write by the `AuditDetails` base.
"""

from __future__ import annotations

from ps_service.audit import AuditDetails, register_audit_action, register_audit_resource_type
from ps_service.invitations.errors import (  # noqa: TC001 -- pydantic resolves the annotation at runtime
    InvitationFailureReason,
)

USER_INVITE_ACTION = "user.invite"
USER_RESOURCE_TYPE = "user"


class UserInviteDetails(AuditDetails):
    """Typed `details` of a `user.invite` audit row: `applied` before the call, `failed` after it.

    Attributes:
        invitee_email: The address the invitation was created for; required.
        reason_code: The enumerated cause of a failed call (`upstream_http_error`,
            `upstream_unreachable` or `unexpected_error`); `None` on the `applied` row.
    """

    invitee_email: str
    reason_code: InvitationFailureReason | None = None


register_audit_action(USER_INVITE_ACTION, UserInviteDetails)
register_audit_resource_type(USER_RESOURCE_TYPE)
