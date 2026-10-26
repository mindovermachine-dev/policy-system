"""Typed `details` models of the `passkey.enroll` and `passkey.revoke` audit actions.

Registers the models with `ps_service.audit.models.register_audit_action` at import time and
registers the `pending_approval` and `principal` audit resource types. `ps_service.passkey_signing`
imports this module, so the actions are registered before the enrolment route or the revoke
command writes its row.

Only a successful enrolment is audited, because it is the one ceremony step whose actor is
attributable: the approval's bound `(sub, iss)`, identified by the approval code rather than by
authentication (hence `proof`). Rejected steps stay in the operator log, and no `passkey.sign`
action exists: a signed action's own component audits its effect.

`passkey.revoke` records the removal of a principal's signing passkeys by the owner passkey
reset: the system actor, resource type `principal`, the revoked subject as resource id.

Side Effects:
    Importing this module registers `passkey.enroll` and `passkey.revoke` as audit actions and
    `pending_approval` and `principal` as audit resource types. Registration is process-global.

Raises:
    ValueError: at import time if the action name is already registered.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ps_service.audit.models import (
    AuditDetails,
    register_audit_action,
    register_audit_resource_type,
)

PASSKEY_ENROLL_ACTION = "passkey.enroll"
PENDING_APPROVAL_RESOURCE_TYPE = "pending_approval"
PASSKEY_REVOKE_ACTION = "passkey.revoke"
PRINCIPAL_RESOURCE_TYPE = "principal"


class PasskeyEnrollDetails(AuditDetails):
    """Details of a `passkey.enroll` audit event.

    Recorded only with `outcome='applied'`, after the credential is stored.

    Attributes:
        proof: How the enrolling caller was identified; always `"approval_code"` (possession of
            the approval's single-use code, not an authenticated session).
    """

    proof: Literal["approval_code"] = "approval_code"


class PasskeyRevokeDetails(AuditDetails):
    """Details of a `passkey.revoke` audit event.

    On `outcome='applied'` the row is written inside the signing transaction before its COMMIT
    and `reason_code` is absent. On `outcome='failed'` (the COMMIT failed after that row was
    written) the counts and ids are those of the transaction that did not commit, and
    `reason_code` says why.

    Attributes:
        revoked_issuer: The identity issuer of the principal whose passkeys were revoked (the
            subject is the row's `resource_id`).
        credentials_deleted: How many signing credentials the revocation deleted, every relying
            party included.
        voided_approval_ids: The ids of the principal's pending approvals the revocation voided.
        reason_code: Why a `failed` row's revocation did not take effect.
    """

    revoked_issuer: str
    credentials_deleted: int = Field(ge=0)
    voided_approval_ids: list[str]
    reason_code: Literal["signing_store_unavailable"] | None = None


register_audit_action(PASSKEY_ENROLL_ACTION, PasskeyEnrollDetails)
register_audit_resource_type(PENDING_APPROVAL_RESOURCE_TYPE)
register_audit_action(PASSKEY_REVOKE_ACTION, PasskeyRevokeDetails)
register_audit_resource_type(PRINCIPAL_RESOURCE_TYPE)
