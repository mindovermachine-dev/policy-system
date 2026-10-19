"""PS Service's own WebAuthn relying party for transaction signing.

Independent of the identity provider's login-time WebAuthn: a separate credential type, a
separate relying-party id (PS Service's own hostname, not the identity provider's issuer path) and
separate storage (`signing_credentials`). When origins are configured
(`ServiceConfig.passkey_signing_allowed_origins`), the first one names the relying party: its
host is the `rp_id` and its scheme and authority the `origin`, whatever `Host` the request
carries, and a request whose `Host` matches none of them is not served. When none is configured,
`rp_id` and `origin` are derived from each request's own `Host` header and scheme, the same
"derive from the request itself" pattern as `ps_service.auth.middleware._resource_metadata_url`,
which works under a local bind, a Kubernetes NodePort and an ingress alike.

Functions: `rp_id_and_origin`, `request_host_is_allowed`, `signable_link_host`,
`enrollment_challenge`, `build_registration_options`, `verify_registration`,
`build_authentication_options` and `verify_authentication`. Verification functions raise the
`webauthn` library's `WebAuthnException` subclasses; the router maps them to one generic
rejection.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

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


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _request_host(request: Request) -> str:
    """Return the request's `Host` header, else the URL's hostname, else an empty string."""
    return request.headers.get("host") or request.url.hostname or ""


def _authority_key(authority: str, scheme: str) -> tuple[str, int] | None:
    """Normalise a `host[:port]` to `(lowercase host without brackets, effective port)`.

    The effective port is the explicit one, else the default of `scheme`. A malformed
    authority yields `None`, which matches nothing.
    """
    try:
        parts = urlsplit(f"//{authority}")
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return parts.hostname, port or _DEFAULT_PORTS.get(scheme, 0)


def _matching_origin(request: Request, origins: Sequence[str]) -> str | None:
    """Return the configured origin whose authority equals the request's `Host`, else `None`.

    Host and origin are compared case-insensitively, with a missing port read as the default of
    the origin's own scheme and an IPv6 literal compared without its brackets. The request's
    own scheme is not consulted.
    """
    host = _request_host(request)
    for origin in origins:
        parts = urlsplit(origin)
        configured = _authority_key(parts.netloc, parts.scheme)
        if configured is not None and configured == _authority_key(host, parts.scheme):
            return origin
    return None


def _configured_origins(request: Request) -> tuple[str, ...]:
    """Return the allowed origins from the running app's configuration."""
    return request.app.state.config.passkey_signing_allowed_origins


def request_host_is_allowed(request: Request) -> bool:
    """Return whether the request's `Host` may be served by the signing ceremony.

    Business Rules:
        With no configured origins every host is allowed. Otherwise the `Host` header must
        equal the authority of at least one configured origin; `X-Forwarded-Host` and
        `Forwarded` are never consulted.

    Args:
        request: The incoming HTTP request.

    Returns:
        `True` when no origins are configured or the host matches one; `False` otherwise.

    Side Effects:
        None.
    """
    origins = _configured_origins(request)
    return not origins or _matching_origin(request, origins) is not None


def rp_id_and_origin(request: Request) -> tuple[str, str]:
    """Return the WebAuthn `(rp_id, origin)` pair for a request.

    `rp_id` is a bare host with any port stripped (a port is never valid in a relying-party
    id). `origin` keeps the port and matches the browser's own origin exactly
    (`scheme://host[:port]`), because the verification compares it verbatim against
    `clientDataJSON.origin`.

    Business Rules:
        With configured origins, `rp_id` is the first origin's host, so it stays stable across
        every configured name, and `origin` is the configured origin the request's `Host`
        matches (the first one when none matches). With none configured, both are derived from
        the request's `Host` header and scheme, and nothing is validated.

    Args:
        request: The incoming HTTP request. Without configured origins its `Host` header is
            used; when absent, the URL's hostname, and when that is absent too, an empty string.

    Returns:
        A tuple `(rp_id, origin)`. An empty host yields an empty `rp_id` when unconfigured.

    Side Effects:
        None.
    """
    origins = _configured_origins(request)
    if origins:
        first = origins[0]
        return urlsplit(first).hostname or "", _matching_origin(request, origins) or first
    host = _request_host(request)
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
