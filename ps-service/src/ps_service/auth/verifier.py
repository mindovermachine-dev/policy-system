"""The one JWT/OIDC verification implementation.

`PsTokenVerifier` is shared, as a single instance, by the REST middleware
(`ps_service.auth.middleware.RestAuthMiddleware`) and the MCP SDK's `MCPServer(token_verifier=...)`
hook -- never two parallel implementations. It structurally satisfies
`mcp.server.auth.provider.TokenVerifier` (a plain class with an
`async def verify_token(self, token: str) -> AccessToken | None` method), so the same object is
handed to both surfaces.

It validates signature, `iss`, `aud`, `exp` and `nbf` through `jwt.PyJWKClient` against the
process's one `AuthContext` (its `jwks_uri` and the discovery-time asymmetric-algorithm
allow-list). `PyJWKClient.get_signing_key_from_jwt` already refetches the key set once on an
unknown `kid`, so no retry logic is needed here.

`verify_token` is also the single logging point for every authentication outcome: REST and MCP
both call this exact method, so both surfaces log identically from one implementation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import jwt
from fastapi.concurrency import run_in_threadpool
from mcp.server.auth.provider import AccessToken

from ps_service.logging.facade import emit_log_entry

if TYPE_CHECKING:
    from ps_service.auth.models import AuthContext
    from ps_service.logging.emitter import LogEmitter

_REQUIRED_CLAIMS: tuple[str, ...] = ("exp", "iss", "aud", "sub")

_COMPONENT = "auth"
_ACTION = "authenticate_request"

_OUTCOME_SUCCESS = "success"
_OUTCOME_UNAUTHENTICATED = "unauthenticated"

# The closed set of rejection reason categories. Every rejection path below logs exactly one of
# these -- never a raw `jwt` exception message.
_REASON_MISSING = "missing"
_REASON_EXPIRED = "expired"
_REASON_WRONG_AUDIENCE = "wrong_audience"
_REASON_WRONG_ISSUER = "wrong_issuer"
_REASON_BAD_ALG = "bad_alg"
_REASON_INVALID_SIGNATURE = "invalid_signature"

# Ordered `(exception type, reason)` pairs `_reason_for_exception` walks via `isinstance`; a
# tuple rather than a dict keyed by class because subclass matching needs `isinstance`.
# Anything not listed falls through to `_REASON_INVALID_SIGNATURE`, the generic catch-all, so no
# unmapped exception's message can escape to a caller.
_EXCEPTION_REASONS: tuple[tuple[type[Exception], str], ...] = (
    (jwt.ExpiredSignatureError, _REASON_EXPIRED),
    # `nbf`/`iat` in the future: there is no separate category, both temporal-validity failures
    # share `expired`.
    (jwt.ImmatureSignatureError, _REASON_EXPIRED),
    (jwt.InvalidAudienceError, _REASON_WRONG_AUDIENCE),
    (jwt.InvalidIssuerError, _REASON_WRONG_ISSUER),
    (jwt.InvalidAlgorithmError, _REASON_BAD_ALG),
    # Untrusted/corrupt signature, a malformed token, or an unknown `kid` that stayed unknown
    # even after `PyJWKClient`'s built-in refetch-once -- all fold into `invalid_signature`.
    (jwt.InvalidSignatureError, _REASON_INVALID_SIGNATURE),
    (jwt.DecodeError, _REASON_INVALID_SIGNATURE),
    (jwt.PyJWKClientError, _REASON_INVALID_SIGNATURE),
)


def _reason_for_exception(exc: Exception) -> str:
    """Map a caught `jwt`/`PyJWKClient` exception to one of the closed rejection reasons.

    Args:
        exc: The exception caught while resolving a signing key or decoding a token.

    Returns:
        One of the six reason categories (`missing`, `expired`, `wrong_audience`, `wrong_issuer`,
        `bad_alg`, `invalid_signature`) -- never the exception's own message. Unmapped exceptions
        yield `invalid_signature`.
    """
    for exc_type, reason in _EXCEPTION_REASONS:
        if isinstance(exc, exc_type):
            return reason
    return _REASON_INVALID_SIGNATURE  # generic catch-all


class PsTokenVerifier:
    """Validate a bearer token against one process-lifetime `AuthContext`.

    Constructed exactly once per `create_app()` call, never at module level: arming the
    module-level MCP server with a real verifier must not happen at bare import time.
    Verification checks the token's `alg` header against the allow-list, then its signature,
    issuer, audience, expiry and not-before time, and requires the `exp`, `iss`, `aud` and `sub`
    claims.
    """

    def __init__(
        self,
        auth_context: AuthContext,
        *,
        jwk_client: jwt.PyJWKClient | None = None,
        emitter: LogEmitter | None = None,
    ) -> None:
        """Build a verifier for `auth_context`.

        Args:
            auth_context: The resolved OIDC configuration (issuer, audience, `jwks_uri`, allowed
                algorithms) every token is checked against.
            jwk_client: Overrides the `jwt.PyJWKClient` built by default (cached key set with a
                300-second lifespan and no refresh cooldown); the seam tests use because
                `PyJWKClient` fetches keys with `urllib` directly. `None` builds the default.
            emitter: Log emitter for authentication entries; `None` uses the process-wide
                default emitter.

        Side Effects:
            None at construction: the default `PyJWKClient` fetches keys lazily on first use.
        """
        self._auth_context = auth_context
        self._jwk_client = jwk_client or jwt.PyJWKClient(
            auth_context.jwks_uri,
            cache_jwk_set=True,
            lifespan=300,
            # PyJWT >=2.14 throttles the unknown-`kid` forced refresh behind a 30s-default
            # cooldown; the refetch-once must happen every time, so the cooldown is disabled.
            cooldown_duration=0,
        )
        self._emitter = emitter

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify `token`, returning an `AccessToken` on success or `None` on any failure.

        Declared `async def` because the MCP SDK's `TokenVerifier` Protocol requires it; the work
        runs on a worker thread so the blocking key-set fetch (on a cache miss) and signature
        verification never block the event loop.

        Business Rules:
            - An empty token is rejected as `missing` before any network or CPU work.
            - A token whose `alg` header is not in the allow-list is rejected as `bad_alg`
              before any key fetch.
            - Every `jwt` failure mode is mapped to a closed reason category; the token and raw
              exception messages never appear in the log entry.

        Args:
            token: The bearer token without the `Bearer ` prefix; empty means none was presented.

        Returns:
            An `AccessToken` carrying the token, the audience as `client_id`, the configured
            scopes, `sub` as `subject` and the full claims; `None` when the token is rejected for
            any reason (the caller answers 401).

        Raises:
            LoggingLifecycleError: No `emitter` was given and no process default emitter is
                configured (a wiring error, not a token failure). Token failures never raise.

        Side Effects:
            Emits one log entry per call: on success it carries component, action and outcome
            only, with no token claim; on rejection it is `outcome=unauthenticated` with a
            `reason` category. May fetch the issuer's key set over the network.
        """
        return await run_in_threadpool(self._verify_token_sync, token)

    def _verify_token_sync(self, token: str) -> AccessToken | None:
        """Verify `token` synchronously (the blocking half of `verify_token`; see its contract)."""
        if not token:
            # No `Authorization` header, a non-`Bearer` scheme, or a `Bearer` header with nothing
            # after it all normalize to an empty string before reaching here: one category,
            # checked first, before any network/CPU cost.
            return self._reject(_REASON_MISSING)

        disallowed_alg_reason = self._disallowed_algorithm_reason(token)
        if disallowed_alg_reason is not None:
            return self._reject(disallowed_alg_reason)

        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self._auth_context.allowed_algorithms),
                audience=self._auth_context.audience,
                issuer=self._auth_context.issuer,
                options={"require": list(_REQUIRED_CLAIMS)},
            )
        except Exception as exc:  # noqa: BLE001 -- still a rejection, never a crash
            # No exception's message may escape to a caller; `_reason_for_exception` maps every
            # `jwt`/`PyJWKClient` failure mode to the closed reason set.
            return self._reject(_reason_for_exception(exc))

        sub = claims.get("sub")
        iss = claims.get("iss")
        if not isinstance(sub, str) or not isinstance(iss, str):
            # `options={"require": [...]}` above only enforces claim presence, not type, so a
            # token could still carry a non-string `sub`/`iss`; reject it (generic category).
            return self._reject(_REASON_INVALID_SIGNATURE)

        emit_log_entry(
            component=_COMPONENT,
            action=_ACTION,
            outcome=_OUTCOME_SUCCESS,
            emitter=self._emitter,
        )
        return AccessToken(
            token=token,
            client_id=self._auth_context.audience,
            scopes=list(self._auth_context.scopes),
            subject=sub,
            claims=claims,
        )

    def _disallowed_algorithm_reason(self, token: str) -> str | None:
        """Return a rejection reason if `token`'s unverified `alg` header isn't allow-listed.

        Checked before any signing key is fetched. The allow-list comes from the issuer's
        advertised algorithms, never from the token's own header, so a disallowed `alg` -- even
        one paired with a `kid` matching a real published key (the RS256/HS256 confusion
        surface) -- never triggers a key-set fetch. `jwt.decode`'s own `algorithms=` list stays
        underneath as defense in depth.

        Args:
            token: A non-empty bearer token.

        Returns:
            `invalid_signature` when the header cannot be parsed, `bad_alg` when `alg` is absent,
            not a string or not allow-listed, else `None` (proceed to signature and claims
            verification).
        """
        try:
            header = jwt.get_unverified_header(token)
        except jwt.DecodeError:
            return _REASON_INVALID_SIGNATURE
        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in self._auth_context.allowed_algorithms:
            return _REASON_BAD_ALG
        return None

    def _reject(self, reason: str) -> None:
        """Log an `outcome=unauthenticated` entry with `reason` and return `None`.

        Args:
            reason: One of the closed six-category reason set (the module-level `_REASON_*`
                constants), never a raw `jwt` exception message or any part of the token.
        """
        emit_log_entry(
            component=_COMPONENT,
            action=_ACTION,
            outcome=_OUTCOME_UNAUTHENTICATED,
            extra={"reason": reason},
            emitter=self._emitter,
        )
