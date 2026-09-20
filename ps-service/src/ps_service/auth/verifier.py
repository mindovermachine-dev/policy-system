"""The one JWT/OIDC verification implementation (issue #58, Slice 3).

``PsTokenVerifier`` is shared, as a single instance, by both the REST
middleware (``ps_service.auth.middleware.RestAuthMiddleware``, this slice)
and the MCP SDK's own ``MCPServer(token_verifier=...)`` hook (Slice 5) --
never two parallel implementations. It structurally satisfies
``mcp.server.auth.provider.TokenVerifier``'s ``Protocol`` (a plain class
with an ``async def verify_token(self, token: str) -> AccessToken | None``
method; no inheritance required), so the exact same object can be handed to
both surfaces.

Validates signature / ``iss`` / ``aud`` / ``exp`` / ``nbf`` via
``jwt.PyJWKClient`` against the process's one ``AuthContext`` (``jwks_uri``
+ the discovery-time asymmetric-algorithm allow-list, AC-BI-009).
``PyJWKClient.get_signing_key_from_jwt`` already implements AC-BI-008's
kid-miss-refetch-once behavior natively -- no hand-rolled retry state
machine is needed here.

``verify_token`` is also THE single logging choke point for every
authentication outcome (issue #58, Slice 10): both REST
(``RestAuthMiddleware``) and MCP (the SDK's ``token_verifier=`` hook) call
this exact method, never a parallel copy, so AC-BI-014/015/016 are
satisfied for both surfaces from one implementation.
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

# AC-BI-015's exhaustive, closed reason-category set (PLAN.md §1.2's mapping
# table). Every rejection path below emits exactly one of these -- never a raw
# `jwt` exception message (AC-BI-004/AC-BI-016).
_REASON_MISSING = "missing"
_REASON_EXPIRED = "expired"
_REASON_WRONG_AUDIENCE = "wrong_audience"
_REASON_WRONG_ISSUER = "wrong_issuer"
_REASON_BAD_ALG = "bad_alg"
_REASON_INVALID_SIGNATURE = "invalid_signature"

# Ordered `(exception type, reason)` pairs `_reason_for_exception` walks via
# `isinstance` -- a plain list rather than a `dict` keyed by exception class,
# since `jwt.InvalidSignatureError` is itself a `jwt.DecodeError` subclass
# (both map to the same reason here, so relative order between the two never
# changes the outcome, but `isinstance` is what a dict-by-type lookup cannot
# express). Exhaustive per PLAN.md §1.2's mapping table; anything not listed
# here falls through to `_REASON_INVALID_SIGNATURE`, the generic catch-all
# (AC-BI-004: no unmapped exception's message may ever escape to a caller).
_EXCEPTION_REASONS: tuple[tuple[type[Exception], str], ...] = (
    (jwt.ExpiredSignatureError, _REASON_EXPIRED),
    # `nbf`/`iat` in the future -- AC-BI-015 enumerates no separate bucket for
    # this; both temporal-validity failures share `expired` (PLAN.md §1.2).
    (jwt.ImmatureSignatureError, _REASON_EXPIRED),
    (jwt.InvalidAudienceError, _REASON_WRONG_AUDIENCE),
    (jwt.InvalidIssuerError, _REASON_WRONG_ISSUER),
    (jwt.InvalidAlgorithmError, _REASON_BAD_ALG),
    # Untrusted/corrupt signature, a malformed token `jwt.get_unverified_header`
    # itself cannot parse, or an unknown `kid` that stayed unknown even after
    # `PyJWKClient`'s built-in refetch-once (AC-BI-008) -- all fold into one
    # `invalid_signature` category.
    (jwt.InvalidSignatureError, _REASON_INVALID_SIGNATURE),
    (jwt.DecodeError, _REASON_INVALID_SIGNATURE),
    (jwt.PyJWKClientError, _REASON_INVALID_SIGNATURE),
)


def _reason_for_exception(exc: Exception) -> str:
    """Map a caught ``jwt``/``PyJWKClient`` exception to AC-BI-015's closed reason set.

    Args:
        exc: The exception ``_verify_token_sync`` caught while resolving a
            signing key or decoding a token.

    Returns:
        One of the six AC-BI-015 reason categories -- never the exception's
        own message (AC-BI-004/AC-BI-016).
    """
    for exc_type, reason in _EXCEPTION_REASONS:
        if isinstance(exc, exc_type):
            return reason
    return _REASON_INVALID_SIGNATURE  # generic catch-all, per PLAN.md §1.2


class PsTokenVerifier:
    """Validate a bearer token against one process-lifetime ``AuthContext``.

    Constructed exactly once per ``create_app()`` call (never module-level:
    the SDK's ``MCPServer`` instance itself is a module-level singleton, but
    arming it with a real verifier -- which would need network I/O for the
    underlying ``PyJWKClient`` were it constructed eagerly -- must not
    happen at bare import time, see ``ps_service.main.create_app``).
    """

    def __init__(
        self,
        auth_context: AuthContext,
        *,
        jwk_client: jwt.PyJWKClient | None = None,
        emitter: LogEmitter | None = None,
    ) -> None:
        """Build a verifier for ``auth_context``.

        Args:
            auth_context: The resolved OIDC configuration (issuer, audience,
                jwks_uri, allowed algorithms) this verifier checks every
                token against.
            jwk_client: Overrides the real ``jwt.PyJWKClient`` this
                constructs by default -- the sole seam tests use, since
                ``PyJWKClient.fetch_data`` calls ``urllib.request.urlopen``
                directly with no ``transport=`` parameter of its own.
            emitter: Overrides the Logging facade's process-wide default
                emitter (``ps_service.logging.facade.emit_log_entry``'s own
                ``emitter=None`` default-to-process-emitter convention,
                mirrored here exactly -- see ``mcp_server.py``'s
                ``handle_mcp_tool_call``). The sole seam audit-logging tests
                use to capture entries without touching the real process
                default.
        """
        self._auth_context = auth_context
        self._jwk_client = jwk_client or jwt.PyJWKClient(
            auth_context.jwks_uri, cache_jwk_set=True, lifespan=300
        )
        self._emitter = emitter

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify ``token``, returning an ``AccessToken`` on success or ``None`` on any failure.

        Declared ``async def`` only because the MCP SDK's ``TokenVerifier``
        Protocol requires it; the body immediately hops to a worker thread
        via ``run_in_threadpool`` so the blocking JWKS fetch (on a cache
        miss) and the blocking RSA/EC signature verification never block
        the event loop -- this is the one place either caller (REST,
        already async; MCP's own ``BearerAuthBackend.authenticate``, also
        already async) does blocking work, so both get non-blocking
        behavior from calling this one method.

        Never raises: every ``jwt`` failure mode is caught and mapped to
        ``None`` (the REST middleware's own ``None``-handling mints the
        401; the MCP SDK's ``BearerAuthBackend`` does the equivalent for
        `/mcp`). Every outcome -- success or rejection -- is also logged
        here (AC-BI-014/015/016, Slice 10): a rejection's log entry never
        contains the token itself or a raw ``jwt`` exception message, only
        a closed-set reason category.
        """
        return await run_in_threadpool(self._verify_token_sync, token)

    def _verify_token_sync(self, token: str) -> AccessToken | None:
        """The blocking half of ``verify_token`` -- runs on a worker thread."""
        if not token:
            # AC-BI-015: no `Authorization` header, a non-`Bearer` scheme, or a
            # `Bearer` header with nothing following it all normalize to an empty
            # string before reaching here (`RestAuthMiddleware._extract_bearer_token`)
            # -- one category, checked first, before any network/CPU cost.
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
            # (AC-BI-004: no exception's message may ever escape to a caller);
            # `_reason_for_exception` maps every `jwt`/`PyJWKClient` failure mode to
            # AC-BI-015's closed reason set, falling back to the generic catch-all.
            return self._reject(_reason_for_exception(exc))

        sub = claims.get("sub")
        iss = claims.get("iss")
        if not isinstance(sub, str) or not isinstance(iss, str):
            # `options={"require": [..., "sub", ...]}` above only enforces claim
            # *presence*, not its type -- a token could still carry a non-string
            # `sub`/`iss`. Treated as a rejection (generic catch-all category, since
            # this is not a `jwt` exception at all), never a crash.
            return self._reject(_REASON_INVALID_SIGNATURE)

        emit_log_entry(
            component=_COMPONENT,
            action=_ACTION,
            outcome=_OUTCOME_SUCCESS,
            extra={"sub": sub, "iss": iss},
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
        """Return a rejection reason if ``token``'s unverified ``alg`` header isn't allow-listed.

        AC-BI-009: checked before ever fetching a signing key. The allow-list
        is computed once at discovery time from the issuer's *advertised*
        algorithms (never derived from the token's own ``alg`` header --
        PyJWT's own documented warning, ``jwt/api_jwt.py``). This short-circuit
        means a disallowed ``alg`` -- including one paired with a ``kid`` that
        matches a real, published signing key (the classic RS256/HS256
        confusion surface) -- never triggers a JWKS network fetch at all.
        ``jwt.decode``'s own ``algorithms=`` allow-list is kept underneath as
        defense-in-depth, never relied on as the sole gate. A token
        ``jwt.get_unverified_header`` itself cannot parse maps to
        ``invalid_signature``, not ``bad_alg`` (there is no header to judge).

        Returns:
            One of AC-BI-015's reason categories if ``token`` must be
            rejected on its ``alg`` alone, else ``None`` (proceed to
            signature/claims verification).
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
        """Emit AC-BI-015's ``outcome=unauthenticated`` entry and return ``None``.

        Args:
            reason: One of the closed six-category reason set (module-level
                ``_REASON_*`` constants) -- never a raw ``jwt`` exception
                message or any part of the token itself (AC-BI-016).
        """
        emit_log_entry(
            component=_COMPONENT,
            action=_ACTION,
            outcome=_OUTCOME_UNAUTHENTICATED,
            extra={"reason": reason},
            emitter=self._emitter,
        )
