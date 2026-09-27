"""ps_service.passkey_signing core types (issue #131, PLAN.md §1.1).

`PendingApprovalRow` mirrors the `pending_approvals` table column-for-column.
A plain frozen dataclass, not a Pydantic model: this shape never crosses a
REST/MCP request/response boundary directly today -- it is
`PendingApprovalStore`'s own return type. Mirrors `ps_service.company_merge.
models`'s own "plain frozen dataclass, not LLM-boundary Pydantic" convention
(that module's docstring).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Literal


@dataclass(frozen=True, slots=True)
class PendingApprovalRow:
    """One row of the `pending_approvals` table (PLAN.md §1.1).

    `code_hash`/`nonce` are raw bytes (`sha256` digest / `secrets.token_bytes`
    output respectively) -- the raw, unhashed capability code itself is never
    part of this shape (L1 "Never log secrets, tokens"); it is returned
    alongside a freshly-created row only by
    `PendingApprovalStore.create_pending_approval`, as a separate return
    value, never persisted.
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
    """One row of the `signing_credentials` table (PLAN.md §1.2, issue #131 Slice 2).

    One row per WebAuthn authenticator an actor has enrolled with PS
    Service's own transaction-signing relying party (never Authentik's
    login-time WebAuthn, PLAN.md §0.3). `public_key` is the COSE public key
    (`credential_public_key` from `webauthn.verify_registration_response`'s
    result) -- no private key, no biometric data, no attestation blob is
    ever part of this shape (AC-BI-007).
    """

    id: str
    actor_subject: str
    actor_issuer: str
    credential_id: bytes
    public_key: bytes
    sign_count: int
    created_at: datetime
