"""AC-BI-009: algorithm allow-list enforcement (issue #58, Slice 7).

The allow-list (`AuthContext.allowed_algorithms`) is computed once at
discovery time from the issuer's *advertised*
`id_token_signing_alg_values_supported` (`ps_service.auth.discovery`) --
never derived from a token's own `alg` header at verification time, per
PyJWT's own documented warning (`jwt/api_jwt.py`: "Do not compute the
`algorithms` parameter based on the `alg` from the token itself"). These
tests prove the allow-list is actually threaded into `PsTokenVerifier` end
to end against a real local HTTP server
(`tests.auth.mock_oidc_provider.MockOidcProvider`, AC-BI-017, whose
discovery document advertises only `["RS256"]`), not just theoretically
true by construction.

`PsTokenVerifier._verify_token_sync` reads the token's unverified header and
rejects any `alg` outside the allow-list *before* ever calling
`jwt.PyJWKClient.get_signing_key_from_jwt` -- so a disallowed `alg` never
triggers a JWKS network fetch at all, closing the classic RS256/HS256
"algorithm confusion" surface (a token whose `kid` names a real, published
key but whose header claims a symmetric `alg`) without needing to inspect
what key that `kid` would have resolved to. `jwt.decode`'s own
`algorithms=` allow-list parameter is kept as defense-in-depth underneath
this pre-check, never relied on as the sole gate.
`mock_oidc_provider.jwks_request_count` is the ground-truth signal
distinguishing "rejected on `alg`, no key fetch" from "rejected on
signature, after a needless key fetch."
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import jwt

from ps_service.auth.models import AuthContext
from ps_service.auth.verifier import PsTokenVerifier
from ps_test_support.mock_oidc_provider import (
    MockOidcProvider,
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging.emitter import LogEmitter

    type MakeEmitter = Callable[..., tuple[LogEmitter, Path]]

_AUDIENCE = "ps-service"
_ALLOWED_ALGORITHMS = frozenset({"RS256"})


def _verifier(provider: MockOidcProvider, *, emitter: LogEmitter) -> PsTokenVerifier:
    auth_context = AuthContext(
        issuer=provider.issuer,
        audience=_AUDIENCE,
        cli_client_id=None,
        scopes=(),
        jwks_uri=provider.jwks_uri,
        allowed_algorithms=_ALLOWED_ALGORITHMS,
    )
    # Slice 10: `verify_token` now always logs (AC-BI-014/015) -- a real
    # `LogEmitter` (`tests/conftest.py`'s `make_emitter`) is required so
    # `emit_log_entry`'s no-default-configured guard never trips here; this
    # file's own tests assert nothing about log content.
    return PsTokenVerifier(auth_context, emitter=emitter)


def _claims(provider: MockOidcProvider, **overrides: object) -> dict[str, object]:
    now = int(time.time())
    claims: dict[str, object] = {
        "sub": "mallory",
        "aud": _AUDIENCE,
        "iss": provider.issuer,
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return claims


def test_genuinely_signed_rs256_token_from_an_untrusted_key_is_rejected_on_signature(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter
) -> None:
    """Sanity check: a correctly-allow-listed `alg` still goes through the JWKS/signature path.

    Distinguishes "rejected because of `alg`" (the cases below, zero JWKS
    hits) from "rejected because of signature" (this case: the `alg` is
    fine, so the verifier legitimately fetches the JWKS looking for a
    matching key, finds none it trusts, and rejects on signature/kid
    grounds instead).
    """
    untrusted_provider = MockOidcProvider()
    try:
        token = untrusted_provider.mint_token(aud=_AUDIENCE, iss=mock_oidc_provider.issuer)
        emitter, _ = make_emitter()
        verifier = _verifier(mock_oidc_provider, emitter=emitter)

        result = asyncio.run(verifier.verify_token(token))
    finally:
        untrusted_provider.shutdown()

    assert result is None
    # The alg (RS256) is allow-listed, so the verifier did attempt a real
    # key lookup before rejecting -- this is the "signature path", not the
    # "alg path" the tests below exercise.
    assert mock_oidc_provider.jwks_request_count == 1


def test_hs256_token_is_rejected_without_ever_fetching_the_jwks(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter
) -> None:
    """A token claiming a symmetric `alg` the issuer never advertised: rejected, no JWKS fetch."""
    emitter, _ = make_emitter()
    verifier = _verifier(mock_oidc_provider, emitter=emitter)
    token = jwt.encode(_claims(mock_oidc_provider), "attacker-controlled-secret", algorithm="HS256")

    result = asyncio.run(verifier.verify_token(token))

    assert result is None
    assert mock_oidc_provider.jwks_request_count == 0


def test_alg_none_token_is_rejected_without_ever_fetching_the_jwks(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter
) -> None:
    """The classic unsecured-JWT attack (`alg: none`): rejected, no JWKS fetch."""
    emitter, _ = make_emitter()
    verifier = _verifier(mock_oidc_provider, emitter=emitter)
    token = jwt.encode(_claims(mock_oidc_provider), key="", algorithm="none")

    result = asyncio.run(verifier.verify_token(token))

    assert result is None
    assert mock_oidc_provider.jwks_request_count == 0


def test_rs256_to_hs256_confusion_attack_with_a_real_kid_is_rejected_without_a_jwks_fetch(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter
) -> None:
    """The classic RS256/HS256 confusion surface: `kid` names a real published key, `alg` is HS256.

    An attacker who has obtained the issuer's real, public JWKS (public by
    definition) can name a genuine `kid` while switching `alg` to a
    symmetric algorithm, hoping a naive verifier resolves the key by `kid`
    first and then verifies using whatever algorithm the token itself
    claims. Here the disallowed `alg` is rejected before any `kid`
    resolution or JWKS fetch happens at all -- the allow-list is the sole
    authority, never the token's own header.
    """
    emitter, _ = make_emitter()
    verifier = _verifier(mock_oidc_provider, emitter=emitter)
    # "key-1" is the mock provider's own initial, genuinely published `kid`
    # (`tests/auth/mock_oidc_provider.py`'s `_keys` seed) -- a real kid, wrong alg.
    token = jwt.encode(
        _claims(mock_oidc_provider),
        "attacker-controlled-secret",
        algorithm="HS256",
        headers={"kid": "key-1"},
    )

    result = asyncio.run(verifier.verify_token(token))

    assert result is None
    assert mock_oidc_provider.jwks_request_count == 0
