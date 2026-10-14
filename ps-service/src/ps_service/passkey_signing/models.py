"""Row types returned by the passkey-signing stores.

`PendingApprovalRow` mirrors the `pending_approvals` table and `SigningCredentialRow` mirrors the
`signing_credentials` table, column for column. Both are plain frozen dataclasses rather than
Pydantic models: they are the stores' own return types and never cross a REST or MCP request or
response boundary directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Literal


@dataclass(frozen=True, slots=True)
class PendingApprovalRow:
    """One privileged action awaiting, or having received, its approver's passkey signature.

    The raw capability code is never part of this shape: only its hash is stored. The code is
    returned once, separately, by `PendingApprovalStore.create_pending_approval` and is never
    persisted.

    Attributes:
        id: Unique approval id; the `{id}` segment of the approval link.
        code_hash: SHA-256 digest of the capability code (raw bytes).
        tool_name: Name of the privileged tool the approval authorises.
        normalized_args: The tool's arguments in canonical form; they are bound into the
            signing challenge.
        actor_subject: Identity subject of the principal who must sign.
        actor_issuer: Identity issuer of that principal.
        nonce: Random bytes (`secrets.token_bytes`) that make the signing challenge unique
            per approval.
        display_summary: Human-readable description of the action shown to the approver.
        status: `pending` until a verified signature consumes the approval, then `signed`.
        outcome: Result of running the action after signing (winner and loser ids, an
            `error` message, or an executor's result); `None` until recorded.
        created_at: When the approval was created (timezone-aware).
        expires_at: When the approval stops being usable (timezone-aware).
    """

    id: str
    code_hash: bytes
    tool_name: str
    normalized_args: dict[str, object]
    actor_subject: str
    actor_issuer: str
    nonce: bytes
    display_summary: dict[str, object]
    status: Literal["pending", "signed"]
    outcome: dict[str, object] | None
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SigningCredentialRow:
    """One WebAuthn authenticator an actor has enrolled with this service's signing relying party.

    The relying party is the service's own, separate from the identity provider's login-time
    WebAuthn. Only public material is held: no private key, biometric data or attestation blob
    is part of this shape.

    Attributes:
        id: Unique row id.
        actor_subject: Identity subject of the principal who owns the credential.
        actor_issuer: Identity issuer of that principal.
        credential_id: The authenticator's credential id (raw bytes); unique across rows.
        public_key: The COSE public key from the verified registration (raw bytes).
        sign_count: Last signature counter reported by the authenticator; a later assertion
            must report a higher value.
        created_at: When the credential was enrolled (timezone-aware).
        rp_id: The WebAuthn relying-party id (host without port) the credential was enrolled
            under. A credential only works for the `rp_id` that created it, so this scopes
            enrolment checks and allowed credentials to the host the browser is on. `None` for a
            row stored before the column existed: its host is unprovable, so it is never offered
            for signing and never counts as enrolment.
    """

    id: str
    actor_subject: str
    actor_issuer: str
    credential_id: bytes
    public_key: bytes
    sign_count: int
    created_at: datetime
    rp_id: str | None = None
    """The relying-party id the credential was enrolled under; `None` when unknown."""
