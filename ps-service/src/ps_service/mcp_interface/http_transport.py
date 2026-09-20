"""Streamable HTTP ASGI transport for MCP Interface (issue #39).

Wraps `mcp_server.server` -- the MCPServer instance `mcp_server.py`
defines -- in the MCP SDK's own Streamable HTTP Starlette sub-app, so the
`cypher` tool and the `psdomain://concepts` resource are reachable from a
client on a different machine
(AC-BI-001/002/003), mounted into the same FastAPI app
`ps_service.main.create_app` builds -- never a second process or port.

Since issue #58 (Slice 5), real per-user authentication is wired here: when
`verifier`/`auth_context` are both given (the local-test bypass, #67, is
inactive), `build_streamable_http_app` arms the module-level `server`
singleton's auth *after* construction, immediately before building the
Starlette sub-app -- `server = MCPServer(...)` itself
(`ps_service.mcp_interface.mcp_server`) keeps zero auth kwargs at import
time, forever (see that module's own scope guard,
`tests/mcp_interface/test_scope_guard.py::test_mcpserver_ctor_has_no_auth_kwargs`).
This reach-through (`server._token_verifier`, `server.settings.auth`) is
necessary because the SDK offers no public "arm auth after construction"
API, and arming it at construction time would mean fail-closed/discovery
I/O running at bare module-import time (poisoning pytest collection for
every test file that merely imports `ps_service.main` transitively) --
mirrors `ps_service/main.py`'s own `_make_verbatim_handler` reach-through
precedent (`main.py:24-27`). `resource_server_url` and `required_scopes`
are deliberately left unset: the former would make the SDK auto-register
its own protected-resource route at the wrong path (nested under `/mcp`
instead of top-level, see this issue's own PLAN.md §0.1); the latter would
silently add scope-based 403 enforcement, which is explicitly out of scope
for this issue (audience check only).
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
    """Build the Streamable HTTP ASGI sub-app exposing `server`'s tool/resource.

    `streamable_http_path="/"` because the mount path itself
    (`MCP_HTTP_MOUNT_PATH`) already supplies the externally visible prefix --
    `ps_service.main.create_app` mounts this app there, giving clients a
    single external endpoint at `MCP_HTTP_MOUNT_PATH + "/"`. `host` is
    threaded through from `ServiceConfig.host` so the SDK's own
    DNS-rebinding protection auto-enables whenever the configured host is
    loopback (always true while the local-test bypass is active --
    AC-BI-004 refuses any other bind -- and true by default otherwise).

    `verifier`/`auth_context` are the exact same instances
    `ps_service.main.create_app` also hands to `RestAuthMiddleware` (one
    shared verifier, one shared `AuthContext`, never two parallel
    implementations). When both are given, `server`'s auth is armed
    immediately before `streamable_http_app(...)` is called -- that call
    reads `self.settings.auth`/`self._token_verifier` lazily each time it
    runs, so mutating them right before this call is sufficient; when
    either is `None` (the local-test bypass is active), `server`'s auth is
    explicitly disarmed (reset to construction-time `None`/`None`), so
    every `/mcp` request is let through unauthenticated -- matching issue
    #67's existing contract precisely.

    This function **unconditionally** sets both attributes on every call,
    in either direction, rather than only arming and never disarming:
    `server` is a true module-level singleton
    (`ps_service.mcp_interface.mcp_server.server`), shared by every
    `create_app()` call in the same process -- including, in the test
    suite, every test in the same `pytest` run. If a bypass-inactive call
    armed real auth and a later, unrelated bypass-active call left the
    singleton's stale `_token_verifier`/`settings.auth` in place instead of
    resetting them, that later call's `/mcp` would silently start requiring
    a token it never asked for, purely as a function of test execution
    order -- a real production process only ever calls this once, so this
    hazard is test-suite-only, but making the mutation fully idempotent per
    call removes the ordering dependency entirely rather than relying on
    tests to always run in a lucky order.
    """
    if verifier is not None and auth_context is not None:
        server.settings.auth = AuthSettings(
            issuer_url=AnyHttpUrl(auth_context.issuer),
            resource_server_url=None,
            required_scopes=None,
        )
        server._token_verifier = verifier  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        # -- the SDK offers no public post-construction auth setter; the
        # decorator-registration requirement in mcp_server.py (see that module's
        # docstring and test_scope_guard.py) forces auth to be armed after
        # MCPServer(...) construction, never at it. Mirrors main.py's identical
        # `_make_verbatim_handler` reach-through precedent.
    else:
        server.settings.auth = None
        server._token_verifier = None  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        # -- explicit disarm, not just "leave whatever a previous call set" -- see
        # this docstring's singleton-pollution paragraph.
    return server.streamable_http_app(streamable_http_path="/", host=host)
