"""Streamable HTTP ASGI transport for MCP Interface (issue #39).

Wraps `mcp_server.server` -- the same MCPServer instance the stdio
entrypoint (`mcp_server.main()`) uses -- in the MCP SDK's own Streamable
HTTP Starlette sub-app, so the `cypher` tool and the `psdomain://concepts`
resource are reachable from a client on a different machine
(AC-BI-001/002/003), mounted into the same FastAPI app
`ps_service.main.create_app` builds -- never a second process or port. No
real per-user authentication is wired here (Group 3, AC-BI-007..011, is
explicitly deferred): the local-test bypass (#67) remains the only auth
path this transport carries. The real substitution point for #65's
eventual real auth is the `MCPServer(...)` constructor call inside
`mcp_server.py` (`token_verifier=`/`auth_server_provider=` kwargs), which
`tests/mcp_interface/test_scope_guard.py::test_mcpserver_ctor_has_no_auth_kwargs`
currently forbids -- wiring in real auth is a change to `mcp_server.py` and
that scope guard, not to this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from ps_service.mcp_interface.mcp_server import server

if TYPE_CHECKING:
    from starlette.applications import Starlette

MCP_HTTP_MOUNT_PATH: Final = "/mcp"


def build_streamable_http_app(*, host: str) -> Starlette:
    """Build the Streamable HTTP ASGI sub-app exposing `server`'s tool/resource.

    `streamable_http_path="/"` because the mount path itself
    (`MCP_HTTP_MOUNT_PATH`) already supplies the externally visible prefix --
    `ps_service.main.create_app` mounts this app there, giving clients a
    single external endpoint at `MCP_HTTP_MOUNT_PATH + "/"`. `host` is
    threaded through from `ServiceConfig.host` so the SDK's own
    DNS-rebinding protection auto-enables whenever the configured host is
    loopback (always true while the local-test bypass is active --
    AC-BI-004 refuses any other bind -- and true by default otherwise).
    """
    return server.streamable_http_app(streamable_http_path="/", host=host)
