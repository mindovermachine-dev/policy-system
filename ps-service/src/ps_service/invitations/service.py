"""Audited invitation orchestration (issue #195).

`invite_user_audited` writes the `user.invite` `applied` row BEFORE the Authentik call
(fail-closed: if the row cannot be written, Authentik is never contacted) and a `failed` row
after a failed call (best-effort, logged). The row carries only the invitee email and, on
failure, an enumerated `reason_code`; the invite's `itoken` and URL are never recorded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.audit import AuditContext, AuditTarget, record_follow_up_row, record_opening_row
from ps_service.invitations.audit_actions import USER_INVITE_ACTION, USER_RESOURCE_TYPE
from ps_service.invitations.client import InvitationResult, create_invitation
from ps_service.invitations.errors import AuthentikInvitationError

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.config import ServiceConfig
    from ps_service.logging.emitter import LogEmitter

_COMPONENT = "invitations"


def invite_user_audited(
    config: ServiceConfig,
    email: str,
    *,
    audit: AuditContext,
    send_invitation: Callable[[ServiceConfig, str], InvitationResult] = create_invitation,
    emitter: LogEmitter | None = None,
) -> InvitationResult:
    """Create an invitation for `email`, auditing it (AC-BI-011/013).

    Raises:
        AuditTrailUnavailableError: the opening row could not be written; nothing was sent.
        AuthentikInvitationError: the Authentik call failed (a `failed` row was attempted).
    """
    target = AuditTarget(
        action=USER_INVITE_ACTION,
        resource_type=USER_RESOURCE_TYPE,
        resource_id=email,
        log_resource_id=False,  # the invitee email is personal data: audited, never logged
    )
    record_opening_row(
        audit,
        target,
        component=_COMPONENT,
        details={"invitee_email": email},
        emitter=emitter,
    )
    try:
        return send_invitation(config, email)
    except AuthentikInvitationError as exc:
        record_follow_up_row(
            audit,
            target,
            component=_COMPONENT,
            outcome="failed",
            details={"invitee_email": email, "reason_code": exc.reason_code},
            emitter=emitter,
        )
        raise
