"""Typed `details` model of the `passkey.enroll` audit action.

Registers the model with `ps_service.audit.models.register_audit_action` at import time and
registers the `pending_approval` audit resource type. `ps_service.passkey_signing` imports this
module, so the action is registered before the enrolment route writes its row.

Only a successful enrolment is audited, because it is the one ceremony step whose actor is
attributable: the approval's bound `(sub, iss)`, identified by the approval code rather than by
authentication (hence `proof`). Rejected steps stay in the operator log, and no `passkey.sign`
action exists: a signed action's own component audits its effect.

Side Effects:
    Importing this module registers `passkey.enroll` as an audit action and `pending_approval`
    as an audit resource type. Registration is process-global.

Raises:
    ValueError: at import time if the action name is already registered.
"""

from __future__ import annotations

from typing import Literal

from ps_service.audit.models import (
    AuditDetails,
    register_audit_action,
    register_audit_resource_type,
)

PASSKEY_ENROLL_ACTION = "passkey.enroll"
PENDING_APPROVAL_RESOURCE_TYPE = "pending_approval"


class PasskeyEnrollDetails(AuditDetails):
    """Details of a `passkey.enroll` audit event.

    Recorded only with `outcome='applied'`, after the credential is stored.

    Attributes:
        proof: How the enrolling caller was identified; always `"approval_code"` (possession of
            the approval's single-use code, not an authenticated session).
    """

    proof: Literal["approval_code"] = "approval_code"


register_audit_action(PASSKEY_ENROLL_ACTION, PasskeyEnrollDetails)
register_audit_resource_type(PENDING_APPROVAL_RESOURCE_TYPE)
