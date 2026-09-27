"""PS Service's own WebAuthn relying party for transaction-signing (issue #131).

Independent of Authentik's login-time WebAuthn relying party (PLAN.md §0.3):
a separate credential type, a separate RP id (PS Service's own hostname, not
Authentik's issuer path), and separate storage (`signing_credentials`,
PLAN.md §1.2). `rp_id`/`rp_name`/`origin` are derived dynamically from each
request's own `Host` header -- mirrors
`ps_service.auth.middleware._resource_metadata_url`'s exact "derived from the
request itself, never hardcoded" pattern (PLAN.md §3) -- so this RP needs no
new config value and works identically under a local dev bind, a `kind`
NodePort, and a prod ClusterIP+Ingress.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import webauthn
from webauthn.helpers.structs import PublicKeyCredentialDescriptor

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi import Request
    from webauthn.authentication.verify_authentication_response import VerifiedAuthentication
    from webauthn.helpers.structs import (
        PublicKeyCredentialCreationOptions,
        PublicKeyCredentialRequestOptions,
    )
    from webauthn.registration.verify_registration_response import VerifiedRegistration

    from ps_service.passkey_signing.models import PendingApprovalRow, SigningCredentialRow

_RP_NAME = "Policy System Transaction Signing"

_ENROLLMENT_CHALLENGE_DOMAIN = b"ps-service:passkey-signing:enroll:"
"""Domain-separates the enrollment challenge from the sign ceremony's own
`tool_name`/`normalized_args`-keyed digest (PLAN.md §3) -- the two must never
collide even for the same `pending_approvals` row."""


def rp_id_and_origin(request: Request) -> tuple[str, str]:
    """Derive `(rp_id, origin)` from the live request's own scheme/host.

    `rp_id` is the bare host with any port stripped (WebAuthn's own
    "effective domain" requirement -- a port is never valid in an `rp_id`);
    `origin` keeps the port, matching the browser's own origin exactly
    (`scheme://host[:port]`), since `verify_registration_response` compares
    it verbatim against `clientDataJSON.origin`.
    """
    host = request.headers.get("host") or request.url.hostname or ""
    rp_id = host.split(":", 1)[0]
    origin = f"{request.url.scheme}://{host}"
    return rp_id, origin


def enrollment_challenge(row: PendingApprovalRow) -> bytes:
    """Deterministically derive this row's one-time WebAuthn registration challenge.

    No `pending_approvals` column stores a registration challenge (PLAN.md
    §1.1 has none for it) -- recomputed fresh, identically, from the row's
    own already-persisted `id`/`nonce` at both `/enroll/options` and
    `/enroll/verify` time. This mirrors the sign ceremony's own
    "recompute, never separately store a challenge column" design (PLAN.md
    §3, AC-BI-011), applied here to enrollment instead of signing.
    """
    return hashlib.sha256(
        _ENROLLMENT_CHALLENGE_DOMAIN + row.id.encode("utf-8") + row.nonce
    ).digest()


def build_registration_options(
    *, request: Request, row: PendingApprovalRow
) -> PublicKeyCredentialCreationOptions:
    """Build this pending approval's WebAuthn registration ("create") options.

    `user_id`/`user_name` are derived from the row's own `actor_subject`
    (PLAN.md §3) -- the same actor identity the pending approval itself is
    bound to, never a separate enrollment-time identity claim.
    """
    rp_id, _origin = rp_id_and_origin(request)
    return webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name=_RP_NAME,
        user_name=row.actor_subject,
        user_id=row.actor_subject.encode("utf-8"),
        challenge=enrollment_challenge(row),
    )


def verify_registration(
    *, request: Request, row: PendingApprovalRow, credential: dict[str, object]
) -> VerifiedRegistration:
    """Verify a `navigator.credentials.create()` response against this row's enrollment challenge.

    Raises `webauthn.helpers.exceptions.InvalidRegistrationResponse` (a
    `WebAuthnException`) on any verification failure -- the caller
    (`passkey_signing.router.post_enroll_verify`) wraps this call in
    `error_handlers.reject_on_webauthn_failure` (issue #131 Slice 4), which
    maps it to the same generic `PendingApprovalInvalidOrExpiredError` every
    other `/approvals/{id}/*` rejection already raises, logging the real
    exception server-side only (AC-BI-015). No AC distinguishes a
    malformed/forged WebAuthn response from any other verification failure,
    so no dedicated wrapper type is introduced for it (mirrors
    IMPL_SLICE_1a's own "no exception type without a real call site"
    discipline, applied here to mean "no *dedicated* type without an AC that
    needs one").
    """
    rp_id, origin = rp_id_and_origin(request)
    return webauthn.verify_registration_response(
        credential=credential,
        expected_challenge=enrollment_challenge(row),
        expected_rp_id=rp_id,
        expected_origin=origin,
    )


def build_authentication_options(
    *, request: Request, challenge: bytes, allow_credentials: Sequence[SigningCredentialRow]
) -> PublicKeyCredentialRequestOptions:
    """Build a pending approval's WebAuthn authentication ("get") options (issue #131 Slice 3).

    `challenge` is the sign ceremony's own recomputed-fresh challenge
    (`passkey_signing.service._compute_sign_challenge`, PLAN.md §3,
    AC-BI-004/AC-BI-011) -- this function never computes it itself, mirroring
    the separation `build_registration_options`/`enrollment_challenge` keep
    for enrollment. `allow_credentials` is every `signing_credentials` row
    already enrolled for this pending approval's own `(actor_subject,
    actor_issuer)` (the caller looks these up via `SigningCredentialStore`).
    """
    rp_id, _origin = rp_id_and_origin(request)
    return webauthn.generate_authentication_options(
        rp_id=rp_id,
        challenge=challenge,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=credential.credential_id)
            for credential in allow_credentials
        ],
    )


def verify_authentication(
    *,
    request: Request,
    challenge: bytes,
    credential: dict[str, object],
    credential_public_key: bytes,
    credential_current_sign_count: int,
) -> VerifiedAuthentication:
    """Verify a `navigator.credentials.get()` response against the recomputed sign challenge.

    `challenge`/`credential_public_key`/`credential_current_sign_count` are
    all supplied by the caller (`passkey_signing.router`), which has already
    performed the actor-identity binding check (AC-BI-005) before calling
    this -- this function only wraps `webauthn.verify_authentication_response`
    with `rp_id`/`origin` derivation, mirroring `verify_registration`'s own
    shape. Raises `webauthn.helpers.exceptions.InvalidAuthenticationResponse`
    (a `WebAuthnException`) on any verification failure (tampered challenge,
    wrong origin, bad signature, non-monotonic `sign_count`) -- the caller
    (`passkey_signing.router.post_sign_verify`) wraps this call in
    `error_handlers.reject_on_webauthn_failure` (issue #131 Slice 4),
    mirroring `verify_registration`'s own identical treatment.
    """
    rp_id, origin = rp_id_and_origin(request)
    return webauthn.verify_authentication_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=rp_id,
        expected_origin=origin,
        credential_public_key=credential_public_key,
        credential_current_sign_count=credential_current_sign_count,
    )
