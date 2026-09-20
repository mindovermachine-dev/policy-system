"""``RestAuthMiddleware`` -- the REST-side OIDC bearer-token gate (issue #58, Slice 3).

A pure ASGI middleware (mirrors ``ps_service.main``'s ``_MaxBodySizeMiddleware``
exactly -- not ``BaseHTTPMiddleware``, for the same reason that class's own
docstring gives: an exception raised from a middleware never reaches a
type-specific ``add_exception_handler`` registration, so a 401 minted here
must build and send its own ``Response`` directly), added via
``app.add_middleware`` **before** ``_MaxBodySizeMiddleware`` is added
(CHANGES.md item 6) so that class stays the outermost, first-to-run layer --
a cheap ``Content-Length`` check rejects an oversized request before any
JWT/RSA verification work happens.

Default-deny: every path not on the explicit open-route allow-list requires
a verified token. ``/mcp*`` is exempted here because the MCP SDK's own
``token_verifier=`` wiring (Slice 5) enforces auth *inside* the mounted
sub-app with the same ``PsTokenVerifier`` instance -- two independent gates
on the same logical resource would mean two different failure-response
shapes for one endpoint, which this design avoids by wiring one verifier at
two integration points instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import status
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers

from ps_service.api.error_handlers import (
    # the one error-body shape this whole API returns; reused so a 401 body
    # matches every other error's shape exactly.
    _error_body,  # pyright: ignore[reportPrivateUsage]
)
from ps_service.auth.models import Principal

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from ps_service.auth.models import AuthContext
    from ps_service.auth.verifier import PsTokenVerifier

_EXEMPT_PATHS = frozenset({"/health", "/ready"})
_EXEMPT_PREFIX = "/.well-known/"
_MCP_MOUNT_PREFIX = "/mcp"

_BEARER_PREFIX = "Bearer "

_UNAUTHENTICATED_CODE = "unauthenticated"
_UNAUTHENTICATED_MESSAGE = "Authentication required."
"""AC-BI-004: a fixed, generic pair -- never the presented token, a key id, or a
``jwt`` exception's message. ``PsTokenVerifier`` never returns *why* a token
failed to this middleware (only ``None``), so there is no failure-detail
value this body could leak even by mistake.
"""

# The AC-BI-010 endpoint itself does not exist until Slice 8 -- this is just the
# URL string a `WWW-Authenticate` header names, per RFC 9728; the header does
# not require the resource it names to already be reachable.
_PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"


def _extract_bearer_token(headers: Headers) -> str | None:
    """Return the bearer token from an ``Authorization`` header, or ``None`` if absent/malformed.

    ``None`` covers: no header at all, a scheme other than ``Bearer``, and a
    ``Bearer`` header with no token following it -- every one of these is
    AC-BI-003's "missing/malformed" case, all folded into a single 401.
    """
    value = headers.get("authorization")
    if value is None or not value.startswith(_BEARER_PREFIX):
        return None
    token = value[len(_BEARER_PREFIX) :].strip()
    return token or None


def _resource_metadata_url(scope: Scope) -> str:
    """Build the ``resource_metadata`` URL for the ``WWW-Authenticate`` header.

    Derived from the request itself (scheme + host), not hardcoded, so it
    works identically under a local dev bind, a ``kind`` NodePort, and a
    prod ClusterIP+Ingress.
    """
    headers = Headers(scope=scope)
    scheme = scope.get("scheme", "http")
    host = headers.get("host", "")
    return f"{scheme}://{host}{_PROTECTED_RESOURCE_PATH}"


class RestAuthMiddleware:
    """Default-deny ASGI middleware: every non-exempt path requires a verified bearer token."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        verifier: PsTokenVerifier | None,
        auth_context: AuthContext | None,
    ) -> None:
        """Wrap ``app``.

        Args:
            app: The downstream ASGI application.
            verifier: The process's one ``PsTokenVerifier``, or ``None`` when
                the local-test bypass (issue #67) is active -- in which case
                every request is let through with no principal bound, matching
                #67's existing unauthenticated contract exactly.
            auth_context: The same instance passed to ``verifier``'s
                construction; unused directly here today, kept for symmetry
                with the MCP-side wiring and for a later slice's use.
        """
        self._app = app
        self._verifier = verifier
        self._auth_context = auth_context

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Reject an unauthenticated/invalid request with a 401 before ``self._app`` ever runs."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = scope["path"]
        if path in _EXEMPT_PATHS or path.startswith((_EXEMPT_PREFIX, _MCP_MOUNT_PREFIX)):
            await self._app(scope, receive, send)
            return

        if self._verifier is None:
            scope["ps_principal"] = None
            await self._app(scope, receive, send)
            return

        token = _extract_bearer_token(Headers(scope=scope))
        # Always call the verifier, even for a missing/malformed header (normalized
        # to ""): `PsTokenVerifier.verify_token` is THE single logging choke point
        # for every authentication outcome (AC-BI-014/015/016, Slice 10), including
        # the `missing` reason category -- an empty string costs nothing (checked
        # first, before any network/CPU work), so this still short-circuits away
        # from JWKS/JWT verification for the common case, just one level deeper.
        access_token = await self._verifier.verify_token(token or "")
        if access_token is None:
            await self._send_401(scope, receive, send)
            return

        sub = access_token.subject
        iss = (access_token.claims or {}).get("iss")
        if not isinstance(sub, str) or not isinstance(iss, str):
            # `PsTokenVerifier.verify_token` only ever returns a non-`None` `AccessToken`
            # with both `sub`/`iss` populated as strings -- this branch defends the type
            # only; it is not a reachable runtime path given today's one verifier.
            await self._send_401(scope, receive, send)
            return

        scope["ps_principal"] = Principal(sub=sub, iss=iss)
        await self._app(scope, receive, send)

    async def _send_401(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Mint and send AC-BI-003/004's 401 body + ``WWW-Authenticate`` header.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive callable.
            send: The ASGI send callable.
        """
        response = JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=_error_body(
                code=_UNAUTHENTICATED_CODE, message=_UNAUTHENTICATED_MESSAGE, run_id=None
            ),
            headers={
                "WWW-Authenticate": f'Bearer resource_metadata="{_resource_metadata_url(scope)}"'
            },
        )
        await response(scope, receive, send)
