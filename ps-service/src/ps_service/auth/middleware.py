"""`RestAuthMiddleware` -- the REST-side OIDC bearer-token gate.

A pure ASGI middleware (not `BaseHTTPMiddleware`): an exception raised from a middleware never
reaches a type-specific `add_exception_handler` registration, so a 401 minted here builds and
sends its own `Response` directly. It is added via `app.add_middleware` before the body-size
middleware so that one stays the outermost, first-to-run layer: a cheap `Content-Length` check
rejects an oversized request before any JWT/RSA verification work happens.

Default-deny: every path not on the explicit open-route allow-list requires a verified token.
`/mcp*` is exempted here because the MCP SDK's own `token_verifier=` wiring enforces auth inside
the mounted sub-app with the same `PsTokenVerifier` instance; two independent gates on one
resource would mean two different failure-response shapes for one endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import status
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers

from ps_service.api.error_handlers import (
    # the one error-body shape this API returns; reused so a 401 body matches every other error.
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
_APPROVALS_PREFIX = "/approvals/"
"""The companion-browser signing ceremony is a bare, unauthenticated capability URL by design:
a plain browser tab carries no `Authorization` header, and the opaque per-approval `code` (never
carried in this path, only in the URL fragment) is the sole per-request authorization, checked
inside `passkey_signing.router` itself.
"""

_BEARER_PREFIX = "Bearer "

_UNAUTHENTICATED_CODE = "unauthenticated"
_UNAUTHENTICATED_MESSAGE = "Authentication required."
"""A fixed, generic pair -- never the presented token, a key id, or a `jwt` exception's message.
`PsTokenVerifier` never tells this middleware why a token failed (only `None`), so there is no
failure detail this body could leak even by mistake.
"""

# The URL a `WWW-Authenticate` header names per RFC 9728 (served by `protected_resource`).
_PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"


def _extract_bearer_token(headers: Headers) -> str | None:
    """Return the bearer token from an `Authorization` header, or `None` if absent/malformed.

    `None` covers no header, a scheme other than `Bearer`, and a `Bearer` header with no token
    after it; all of these are folded into a single 401.
    """
    value = headers.get("authorization")
    if value is None or not value.startswith(_BEARER_PREFIX):
        return None
    token = value[len(_BEARER_PREFIX) :].strip()
    return token or None


def _resource_metadata_url(scope: Scope) -> str:
    """Build the `resource_metadata` URL for the `WWW-Authenticate` header.

    Derived from the request itself (scheme + host), not hardcoded, so it works identically under
    a local dev bind, a `kind` NodePort, and a production ClusterIP plus Ingress.
    """
    headers = Headers(scope=scope)
    scheme = scope.get("scheme", "http")
    host = headers.get("host", "")
    return f"{scheme}://{host}{_PROTECTED_RESOURCE_PATH}"


class RestAuthMiddleware:
    """Default-deny ASGI middleware: every non-exempt path requires a verified bearer token.

    Exempt: `/health`, `/ready`, anything under `/.well-known/`, `/approvals/` (the signing
    ceremony's own capability-URL check) and `/mcp` (gated by the MCP SDK with the same verifier).
    For every other HTTP request the bearer token is verified; success binds a `Principal` to
    `scope["ps_principal"]`, failure answers 401 and the wrapped application never runs.
    Non-HTTP scopes (e.g. lifespan) pass through untouched.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        verifier: PsTokenVerifier | None,
        auth_context: AuthContext | None,
    ) -> None:
        """Wrap `app` with the bearer-token gate.

        Args:
            app: The downstream ASGI application.
            verifier: The process's one `PsTokenVerifier`, or `None` when the local-test bypass is
                active -- in which case every request is let through with `ps_principal` set to
                `None`.
            auth_context: The `AuthContext` the verifier was built from, or `None` under the
                bypass; stored for symmetry with the MCP-side wiring and not read by this class.

        Side Effects:
            None; the instance only stores its arguments.
        """
        self._app = app
        self._verifier = verifier
        self._auth_context = auth_context

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Gate one ASGI connection: pass it on if authorised, otherwise answer 401.

        Args:
            scope: The ASGI connection scope; for an authorised request `ps_principal` is added.
            receive: The ASGI receive callable, passed through.
            send: The ASGI send callable, passed through or used to send the 401.

        Returns:
            `None`; the response is delivered through `send`.

        Raises:
            LoggingLifecycleError: Propagated from the verifier when no process log emitter is
                configured (a wiring error, not a request failure).

        Side Effects:
            Calls `PsTokenVerifier.verify_token`, which emits one authentication log entry per
            verified request. Sets `scope["ps_principal"]` to a `Principal` (verified) or `None`
            (bypass). Sends a 401 response with a `WWW-Authenticate` header on failure.
        """
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = scope["path"]
        if path in _EXEMPT_PATHS or path.startswith(
            (_EXEMPT_PREFIX, _APPROVALS_PREFIX, _MCP_MOUNT_PREFIX)
        ):
            await self._app(scope, receive, send)
            return

        if self._verifier is None:
            scope["ps_principal"] = None
            await self._app(scope, receive, send)
            return

        token = _extract_bearer_token(Headers(scope=scope))
        # Always call the verifier, even for a missing/malformed header (normalized to ""):
        # `PsTokenVerifier.verify_token` is the single logging point for every authentication
        # outcome, including `missing`; an empty token is rejected there before any network/CPU
        # work, so nothing is lost.
        access_token = await self._verifier.verify_token(token or "")
        if access_token is None:
            await self._send_401(scope, receive, send)
            return

        sub = access_token.subject
        iss = (access_token.claims or {}).get("iss")
        if not isinstance(sub, str) or not isinstance(iss, str):
            # `PsTokenVerifier.verify_token` only returns an `AccessToken` with `sub`/`iss`
            # populated as strings; this branch narrows the types for the type checker.
            await self._send_401(scope, receive, send)
            return

        scope["ps_principal"] = Principal(sub=sub, iss=iss)
        await self._app(scope, receive, send)

    async def _send_401(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the fixed 401 body with a `WWW-Authenticate` header naming the metadata URL.

        Args:
            scope: The ASGI connection scope (used to derive the metadata URL).
            receive: The ASGI receive callable.
            send: The ASGI send callable the response is written to.

        Side Effects:
            Writes a complete 401 JSON response to `send`.
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
