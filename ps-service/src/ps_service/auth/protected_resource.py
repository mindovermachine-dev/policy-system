"""RFC 9728 OAuth 2.0 Protected Resource Metadata routes.

Publishes `GET /.well-known/oauth-protected-resource`, the URL the `WWW-Authenticate:
Bearer resource_metadata="..."` header of `RestAuthMiddleware` points at, plus the path-specific
form for the MCP endpoint. Both are registered directly on the application by
`ps_service.main.create_app`, not through the business-logic API router.

Deliberately unauthenticated: `RestAuthMiddleware` exempts everything under `/.well-known/`, so
reachability without an `Authorization` header is a property of that allow-list, not of this
module.

Never registered inside the MCP mount (`/mcp`): the MCP SDK's auth is armed without a
`resource_server_url`, so the SDK never adds a competing protected-resource route there.
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
    """Build the metadata document for `resource` from the application's `AuthContext`.

    With no `AuthContext` (local-test bypass) the issuer and scope lists are empty and the CLI
    client id is `None`.
    """
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
    """Return `scheme://host` as the client addressed the service (the `Host` header wins)."""
    host = request.headers.get("host", request.url.netloc)
    return f"{request.url.scheme}://{host}"


def protected_resource_metadata(request: Request) -> ProtectedResourceMetadata:
    """Return the RFC 9728 protected-resource metadata for authenticating to this service.

    `resource` is derived from the live request (scheme + host), never hardcoded, so it is correct
    under a local bind, a NodePort, and behind a TLS-terminating ingress alike. The document
    names the OIDC issuer as the authorization server and lists the advertised scopes. When the
    service runs in the local-test bypass there is no issuer, so the server and scope lists are
    empty rather than the call failing.

    Args:
        request: The incoming request; the service's authentication configuration is read from
            its application state.

    Returns:
        The metadata document: `resource` (this service's origin), `authorization_servers`,
        `scopes_supported` and, only when configured, `ps_cli_client_id` (omitted from the
        response body when unset).

    Raises:
        Nothing: the document is built from configuration and never fails.

    Side Effects:
        None; read-only and unauthenticated.
    """
    return _build_metadata(request, _origin(request))


def protected_resource_metadata_for_path(
    request: Request, resource_path: str
) -> ProtectedResourceMetadata:
    """Return the path-specific RFC 9728 metadata document for the MCP endpoint.

    RFC 9728 inserts the resource's path after `/.well-known/oauth-protected-resource`, so a
    client given the MCP URL `https://<host>/mcp/` asks for
    `/.well-known/oauth-protected-resource/mcp/` and requires the document's `resource` to equal
    that URL. Only the MCP mount is a resource with its own path; any other suffix is a 404, so
    the route never reflects arbitrary client-chosen paths into `resource`.

    Args:
        request: The incoming request; the service's authentication configuration is read from
            its application state.
        resource_path: The path after the well-known prefix, without the leading slash; a
            trailing slash is accepted and echoed into `resource`.

    Returns:
        The metadata document whose `resource` is the MCP URL as the client addressed it, with
        the same other fields as `protected_resource_metadata`.

    Raises:
        HTTPException: 404 when `resource_path` is not the MCP mount.

    Side Effects:
        None; read-only and unauthenticated.
    """
    if resource_path.rstrip("/") != MCP_RESOURCE_PATH:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    return _build_metadata(request, f"{_origin(request)}/{resource_path}")
