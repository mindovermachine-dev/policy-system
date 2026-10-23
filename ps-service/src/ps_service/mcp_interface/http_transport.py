"""Streamable HTTP ASGI transport for the MCP Interface.

Wraps `mcp_server.server` (the `MCPServer` instance that `tool_core.py` defines and the front
door re-exports) in the MCP SDK's
Streamable HTTP Starlette sub-app, so every registered tool and the `psdomain://concepts` resource
are reachable from a client on a different machine. The sub-app is mounted into the same FastAPI
app that `ps_service.main.create_app` builds, never into a second process or port.

When a token verifier and an authentication context are both supplied (the local-test bypass is
inactive), `build_streamable_http_app` arms the module-level `server` singleton's authentication
after construction, immediately before building the Starlette sub-app. `server = MCPServer(...)`
itself keeps no authentication arguments at import time. Arming goes through the private attributes
`server._token_verifier` and `server.settings.auth`, because the SDK offers no public way to arm
authentication after construction, and arming at construction would run fail-closed discovery I/O at
bare module import, which would break every test module that merely imports `ps_service.main`.
`resource_server_url` and `required_scopes` are deliberately left unset: the former would make the
SDK register its own protected-resource route nested under `/mcp` instead of at the top level, and
the latter would add scope-based 403 enforcement, while only the audience check is intended.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from ps_service.mcp_interface.mcp_server import server

if TYPE_CHECKING:
    from starlette.applications import Starlette

    from ps_service.auth.models import AuthContext
    from ps_service.auth.verifier import PsTokenVerifier

MCP_HTTP_MOUNT_PATH: Final = "/mcp"


def build_streamable_http_app(
    *, host: str, verifier: PsTokenVerifier | None, auth_context: AuthContext | None
) -> Starlette:
    """Build the Streamable HTTP ASGI sub-app that exposes `server`'s tools and resource.

    The sub-app serves at `streamable_http_path="/"`, because the mount path (`MCP_HTTP_MOUNT_PATH`)
    already supplies the external prefix; mounted there, clients see a single endpoint at
    `MCP_HTTP_MOUNT_PATH + "/"`. When both `verifier` and `auth_context` are given, `server`'s
    authentication is armed immediately before `streamable_http_app(...)` is called; that call reads
    `server.settings.auth` and `server._token_verifier` each time it runs, so mutating them just
    before
    it is sufficient. When either is `None` (the local-test bypass is active), `server`'s
    authentication
    is explicitly disarmed, so every `/mcp` request is let through unauthenticated.

    Both attributes are set unconditionally on every call, in either direction. `server` is a
    module-level singleton shared by every `create_app()` call in the same process; if a call that
    armed
    authentication were followed by a bypass-active call that left the stale verifier in place, the
    later
    call's `/mcp` would silently require a token it never asked for, depending only on call order. A
    production process calls this once, so the hazard is mainly in test runs, where resetting on
    every
    call removes the order dependency.

    Args:
        host: Bind host from the service configuration, passed to the SDK so its DNS-rebinding
            protection enables whenever the host is loopback (always the case while the local-test
            bypass is active, and the default otherwise).
        verifier: The token verifier that the REST authentication middleware also uses (one shared
            verifier), or `None` when the local-test bypass is active.
        auth_context: The authentication context shared with the REST middleware (issuer and related
            settings), or `None` when the local-test bypass is active.

    Returns:
        The Starlette sub-app for the MCP Streamable HTTP endpoint, for mounting at
        `MCP_HTTP_MOUNT_PATH`.

    Side Effects:
        Mutates the shared `server` singleton: sets or clears `server.settings.auth` and
        `server._token_verifier`.
    """
    if verifier is not None and auth_context is not None:
        server.settings.auth = AuthSettings(
            issuer_url=AnyHttpUrl(auth_context.issuer),
            resource_server_url=None,
            required_scopes=None,
        )
        server._token_verifier = verifier  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        # -- the SDK offers no public post-construction auth setter, and tools are registered
        # by decorator at import time, so auth must be armed after MCPServer(...) construction,
        # never at it.
    else:
        server.settings.auth = None
        server._token_verifier = None  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        # -- explicit disarm rather than leaving whatever a previous call set (see the
        # function docstring's paragraph on the shared singleton).
    return server.streamable_http_app(streamable_http_path="/", host=host)
