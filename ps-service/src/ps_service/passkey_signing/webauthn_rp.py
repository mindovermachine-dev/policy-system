"""PS Service's own WebAuthn relying party for transaction signing.

Independent of the identity provider's login-time WebAuthn: a separate credential type, a
separate relying-party id (PS Service's own hostname, not the identity provider's issuer path) and
separate storage (`signing_credentials`). `rp_id` and `origin` are derived from each request's own
`Host` header and scheme, never hardcoded, the same "derive from the request itself" pattern as
`ps_service.auth.middleware._resource_metadata_url`. The relying party therefore needs no
configuration value and works under a local bind, a Kubernetes NodePort and a production ingress
alike.

Functions: `rp_id_and_origin`, `signable_link_host`, `enrollment_challenge`,
`build_registration_options`, `verify_registration`, `build_authentication_options` and
`verify_authentication`. Verification functions raise the `webauthn` library's
`WebAuthnException` subclasses; the router maps them to one generic rejection.
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
"""Domain-separates the enrollment challenge from the signing ceremony's own challenge.

The signing challenge is a digest keyed on `tool_name` and `normalized_args`; the two must never
collide, even for the same `pending_approvals` row.
"""


def rp_id_and_origin(request: Request) -> tuple[str, str]:
    """Derive the WebAuthn `(rp_id, origin)` pair from the live request's scheme and host.

    `rp_id` is the bare host with any port stripped (a port is never valid in a relying-party
    id). `origin` keeps the port and matches the browser's own origin exactly
    (`scheme://host[:port]`), because the verification compares it verbatim against
    `clientDataJSON.origin`.

    Args:
        request: The incoming HTTP request. Its `Host` header is used; when absent, the URL's
            hostname, and when that is absent too, an empty string.

    Returns:
        A tuple `(rp_id, origin)`. Nothing is validated: an empty host yields an empty `rp_id`.

    Side Effects:
        None.
    """
    host = request.headers.get("host") or request.url.hostname or ""
    rp_id = host.split(":", 1)[0]
    origin = f"{request.url.scheme}://{host}"
    return rp_id, origin


_LOOPBACK_IP_LITERALS = frozenset({"127.0.0.1", "::1"})
"""The two loopback spellings that are not domains, and so are rejected as a relying-party id.

`localhost`, the third loopback spelling, is already valid and is what both map onto.
"""

_SIGNABLE_LOOPBACK_HOST = "localhost"


def signable_link_host(netloc: str) -> str:
    """Return `netloc` with a loopback IP literal rewritten to `localhost`.

    A WebAuthn relying-party id must be a domain, so a browser raises `SecurityError` when a
    credential is created on a `127.0.0.1` or `::1` host, which makes an approval link built off
    such a host unsignable. `rp_id_and_origin` deliberately does not apply this substitution: it
    must derive both values verbatim from the host the browser actually used, because `origin` is
    compared byte for byte against `clientDataJSON.origin`. The substitution belongs where a link
    is first built, so the browser is pointed at a signable spelling of the same machine.

    Business Rules:
        Only loopback literals are rewritten. Any other IP host (a NodePort, a LAN address) names
        a machine `localhost` would not reach, so it is returned unchanged. It stays unsignable
        and the companion-browser page says so.

    Args:
        netloc: A `host[:port]` as it appears in a live request, including the bracketed IPv6
            form (`[::1]:8000`).

    Returns:
        The `netloc` to put in an approval link, port preserved: `localhost[:port]` for a
        loopback IP literal, otherwise `netloc` unchanged.

    Side Effects:
        None.
    """
    if netloc.startswith("["):
        bracketed_host, _, port = netloc.partition("]")
        host = bracketed_host.removeprefix("[")
    else:
        host, _, raw_port = netloc.partition(":")
        port = f":{raw_port}" if raw_port else ""
    if host not in _LOOPBACK_IP_LITERALS:
        return netloc
    return f"{_SIGNABLE_LOOPBACK_HOST}{port}"


def enrollment_challenge(row: PendingApprovalRow) -> bytes:
    """Derive the one-time WebAuthn registration challenge for a pending approval.

    No column stores the registration challenge. It is recomputed identically at `/enroll/options`
    and `/enroll/verify` time from the row's persisted `id` and `nonce` (SHA-256 of a fixed domain
    prefix, the id and the nonce), the same recompute-never-store design as the signing challenge.

    Args:
        row: The pending approval being enrolled for.

    Returns:
        The 32-byte SHA-256 digest used as the registration challenge. Deterministic for a row.

    Side Effects:
        None; pure computation.
    """
    return hashlib.sha256(
        _ENROLLMENT_CHALLENGE_DOMAIN + row.id.encode("utf-8") + row.nonce
    ).digest()


def build_registration_options(
    *, request: Request, row: PendingApprovalRow
) -> PublicKeyCredentialCreationOptions:
    """Build the WebAuthn registration ("create") options for a pending approval.

    `user_id` and `user_name` come from the row's own `actor_subject`, the identity the pending
    approval is bound to, never from a separate claim made at enrollment time.

    Args:
        request: The incoming HTTP request; its host yields the relying-party id.
        row: The pending approval being enrolled for; supplies the challenge and the user.

    Returns:
        The `PublicKeyCredentialCreationOptions` to send to the browser.

    Side Effects:
        None.
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
    """Verify a `navigator.credentials.create()` response against a row's enrollment challenge.

    Expected challenge, relying-party id and origin are all recomputed from `row` and `request`.

    Args:
        request: The incoming HTTP request; its scheme and host give the expected origin.
        row: The pending approval being enrolled for.
        credential: The browser's registration response as parsed JSON.

    Returns:
        The library's `VerifiedRegistration` (credential id, public key, sign count).

    Raises:
        webauthn.helpers.exceptions.InvalidRegistrationResponse: verification failed (tampered
            challenge, wrong origin or relying party, malformed or forged response). A
            `WebAuthnException`; the router maps it to the generic invalid-or-expired rejection
            and logs the real error server-side only.

    Side Effects:
        None.
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
    """Build the WebAuthn authentication ("get") options for a pending approval.

    This function never computes the challenge itself: the caller passes the signing challenge
    (`passkey_signing.service._compute_sign_challenge`), keeping challenge derivation separate from
    options building as `build_registration_options` does for enrollment.

    Args:
        request: The incoming HTTP request; its host yields the relying-party id.
        challenge: The signing challenge bytes for the pending approval.
        allow_credentials: The credentials already enrolled for the approval's own
            `(actor_subject, actor_issuer)` under this relying-party id; each is advertised in
            `allowCredentials`. May be empty.

    Returns:
        The `PublicKeyCredentialRequestOptions` to send to the browser.

    Side Effects:
        None.
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
    """Verify a `navigator.credentials.get()` response against the recomputed signing challenge.

    The caller has already checked that the credential belongs to the approval's own actor; this
    function wraps the library verification with relying-party id and origin derived from
    `request`.

    Args:
        request: The incoming HTTP request; its scheme and host give the expected origin.
        challenge: The signing challenge bytes for the pending approval.
        credential: The browser's authentication response as parsed JSON.
        credential_public_key: The enrolled credential's public key.
        credential_current_sign_count: The signature counter currently stored for the credential.

    Returns:
        The library's `VerifiedAuthentication`, including the new sign count to persist.

    Raises:
        webauthn.helpers.exceptions.InvalidAuthenticationResponse: verification failed (tampered
            challenge, wrong origin, bad signature, non-monotonic sign count). A
            `WebAuthnException`; the router maps it to the generic invalid-or-expired rejection
            and logs the real error server-side only.

    Side Effects:
        None.
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
