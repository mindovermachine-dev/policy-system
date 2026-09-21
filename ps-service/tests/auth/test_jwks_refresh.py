"""AC-BI-008: JWKS kid-miss refetch-once (issue #58, Slice 6).

`PsTokenVerifier` delegates key lookup to `jwt.PyJWKClient.get_signing_key_from_jwt`,
which already implements "on a cached-`kid` miss, refresh the JWK Set once and
retry" natively (PLAN.md §0.2, `jwt/jwks_client.py::get_signing_key`) -- no
hand-rolled retry state machine exists anywhere in this codebase. These tests
prove that integration actually happens end to end against a real local HTTP
server (`tests.auth.mock_oidc_provider.MockOidcProvider`, AC-BI-017), not just
theoretically true by construction: a verifier whose cache was primed with the
provider's *original* key must still successfully validate a token signed
with a key added *after* that priming fetch (`rotate_key()`), by refetching
exactly once -- and must still reject, after exactly one refetch attempt (not
a retry loop), a token signed with a `kid` the provider never published at
all.

Exercises `PsTokenVerifier` directly (not through `RestAuthMiddleware`/
`create_app`) -- Slices 3/4 already prove the REST 401/200 plumbing around
this verifier end to end; this file's job is narrowly the JWKS-refresh
integration itself, using `MockOidcProvider.jwks_request_count` as the one
ground-truth signal for "how many times was `/jwks.json` actually fetched."
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ps_service.auth.models import AuthContext
from ps_service.auth.verifier import PsTokenVerifier
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging.emitter import LogEmitter
    from ps_test_support.mock_oidc_provider import MockOidcProvider

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


def test_token_signed_by_a_key_rotated_in_after_the_initial_fetch_still_validates(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter
) -> None:
    """A `kid` absent from the verifier's cached JWKS is found by exactly one refetch."""
    emitter, _ = make_emitter()
    verifier = _verifier(mock_oidc_provider, emitter=emitter)

    # Prime the verifier's JWKS cache while only the original key exists.
    priming_token = mock_oidc_provider.mint_token(sub="alice", aud=_AUDIENCE)
    primed = asyncio.run(verifier.verify_token(priming_token))
    assert primed is not None
    assert mock_oidc_provider.jwks_request_count == 1

    # A key added *after* that priming fetch -- genuinely absent from the
    # verifier's cache, present only in the provider's live JWKS document.
    new_kid = mock_oidc_provider.rotate_key()
    token_from_new_key = mock_oidc_provider.mint_token(sub="bob", aud=_AUDIENCE, kid=new_kid)

    result = asyncio.run(verifier.verify_token(token_from_new_key))

    assert result is not None
    assert result.subject == "bob"
    # Exactly one additional fetch -- the built-in refetch-once, not zero
    # (which would mean the miss was never detected) and not more than one.
    assert mock_oidc_provider.jwks_request_count == 2


def test_kid_absent_even_after_refetch_is_rejected_without_a_retry_loop(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter
) -> None:
    """A `kid` the provider never published at all: rejected, after exactly one refetch."""
    emitter, _ = make_emitter()
    verifier = _verifier(mock_oidc_provider, emitter=emitter)

    # Prime the cache, same as above, so this test also proves a *subsequent*
    # miss (not just a first-ever lookup) still refetches exactly once.
    priming_token = mock_oidc_provider.mint_token(sub="alice", aud=_AUDIENCE)
    assert asyncio.run(verifier.verify_token(priming_token)) is not None
    assert mock_oidc_provider.jwks_request_count == 1

    never_published_token = mock_oidc_provider.mint_token(
        sub="mallory", aud=_AUDIENCE, kid="never-published-kid"
    )

    result = asyncio.run(verifier.verify_token(never_published_token))

    assert result is None
    # One refetch attempt, then a clean rejection -- not a retry loop (which
    # would keep incrementing this counter beyond a single extra fetch).
    assert mock_oidc_provider.jwks_request_count == 2
