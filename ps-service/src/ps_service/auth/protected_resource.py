"""RFC 9728 OAuth 2.0 Protected Resource Metadata endpoint (issue #58, Slice 8, AC-BI-010).

Publishes `GET /.well-known/oauth-protected-resource`, the exact URL
`RestAuthMiddleware`'s own `WWW-Authenticate: Bearer resource_metadata="..."`
header (Slice 3, `ps_service.auth.middleware._resource_metadata_url`) already
points at. Registered directly on `app` alongside `/health`/`/ready`
(`ps_service/main.py`) via `app.add_api_route`, not through
`build_api_router()` -- a bare discovery document, not a business-logic route.

Deliberately unauthenticated: `RestAuthMiddleware`'s `_EXEMPT_PREFIX =
"/.well-known/"` (Slice 3) already covers this exact path, so no separate
opt-out is needed here -- reachability with no `Authorization` header is a
property of the middleware's allow-list, not of this module.

Never registered inside the MCP mount (`/mcp`): `AuthSettings.resource_server_url`
is left `None` when arming the MCP SDK's auth (Slice 5, PLAN.md §0.1), so the
SDK never auto-registers a competing protected-resource route under `/mcp` --
this module's route is the only one that exists, at the top level only.
"""

from __future__ import annotations

from fastapi import (  # FastAPI needs Request at runtime (else a 422 body field)
    HTTPException,
    Request,
    status,
)

from ps_service.api.models import ProtectedResourceMetadata

MCP_RESOURCE_PATH = "mcp"


def _build_metadata(request: Request, resource: str) -> ProtectedResourceMetadata:
    auth_context = request.app.state.auth_context
    if auth_context is None:
        return ProtectedResourceMetadata(
            resource=resource,
            authorization_servers=[],
            scopes_supported=[],
            ps_cli_client_id=None,
        )
    return ProtectedResourceMetadata(
        resource=resource,
        authorization_servers=[auth_context.issuer],
        scopes_supported=list(auth_context.scopes),
        ps_cli_client_id=auth_context.cli_client_id,
    )


def _origin(request: Request) -> str:
    host = request.headers.get("host", request.url.netloc)
    return f"{request.url.scheme}://{host}"


def protected_resource_metadata(request: Request) -> ProtectedResourceMetadata:
    """Build the RFC 9728 protected-resource-metadata document for this request.

    `resource` is derived from the live request (scheme + host), never
    hardcoded, mirroring `RestAuthMiddleware._resource_metadata_url`'s exact
    convention -- correct under a local dev bind, a `kind` NodePort, and a
    prod ClusterIP+Ingress alike (behind the Ingress the scheme is `https`
    because `main()` runs uvicorn with the proxy's forwarded headers trusted).

    When the local-test bypass (#67) is active, `request.app.state.auth_context`
    is `None` -- there is no issuer to report, so `authorization_servers`/
    `scopes_supported` degrade to empty lists rather than 404/500ing (this is
    a deliberate, documented degenerate case, not an oversight). `ps_cli_client_id`
    is always passed through as `None` in that branch too, which
    `response_model_exclude_none=True` on this route's registration
    (`ps_service/main.py`) then omits entirely from the response body.

    Args:
        request: The incoming request (FastAPI injects it).

    Returns:
        The RFC 9728 metadata document for this PS Service instance.
    """
    return _build_metadata(request, _origin(request))


def protected_resource_metadata_for_path(
    request: Request, resource_path: str
) -> ProtectedResourceMetadata:
    """Build the path-specific RFC 9728 document for the MCP endpoint (issue #237).

    RFC 9728 inserts the resource's path after `/.well-known/oauth-protected-resource`, so a
    client given the MCP URL `https://<host>/mcp/` asks for
    `/.well-known/oauth-protected-resource/mcp/` and requires the document's `resource` to equal
    that URL. Only the MCP mount is a resource with its own path; any other suffix is a 404, so
    the route never reflects arbitrary client-chosen paths into `resource`.

    Args:
        request: The incoming request (FastAPI injects it).
        resource_path: The path after the well-known prefix, without the leading slash.

    Returns:
        The metadata document whose `resource` is the MCP URL as the client addressed it.

    Raises:
        HTTPException: 404 when `resource_path` is not the MCP mount.
    """
    if resource_path.rstrip("/") != MCP_RESOURCE_PATH:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    return _build_metadata(request, f"{_origin(request)}/{resource_path}")
