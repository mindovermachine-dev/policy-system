"""Audited invitation orchestration.

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
    """Create an invitation for `email`, auditing the attempt.

    Business Rules:
        - The `applied` row is written before Authentik is contacted (fail closed): if it cannot
          be written, no invitation is created.
        - A failed call writes a best-effort `failed` row with the enumerated reason; failing to
          write that row is logged and does not mask the invitation error.
        - The audit row and logs hold only the invitee email (the log omits it); the `itoken`
          and invite URL are never recorded.

    Args:
        config: Service configuration supplying the Authentik credential and URLs.
        email: The invitee's email address; recorded as the audit resource id.
        audit: The acting principal and the audit store receiving the rows.
        send_invitation: Callable creating the invitation; defaults to `create_invitation`.
            Tests inject a fake.
        emitter: Log emitter for audit-failure entries; `None` uses the process default.

    Returns:
        The `InvitationResult` (`itoken` and `invite_url`) of the created invitation.

    Raises:
        AuditTrailUnavailableError: The opening row could not be written; nothing was sent.
        AuthentikInvitationError: The Authentik call failed; a `failed` row was attempted before
            re-raising.

    Side Effects:
        Writes a `user.invite` audit row with outcome `applied`, creates an invitation in
        Authentik, and on failure writes a second row with outcome `failed` and a `reason_code`.
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
