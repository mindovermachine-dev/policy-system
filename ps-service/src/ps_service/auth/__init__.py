"""ps_service.auth -- OIDC bearer-token authentication for the REST and MCP surfaces.

One shared component resolves the OIDC configuration at startup and verifies bearer tokens:

- `resolve_auth_context` fails closed at startup (or returns `None` under the local-test bypass),
  producing the process-lifetime `AuthContext`.
- `PsTokenVerifier` validates a token's signature, issuer, audience, expiry and not-before time and
  is shared by the REST middleware and the MCP `token_verifier=` hook.
- `RestAuthMiddleware` is the REST-side default-deny gate that answers 401 for unauthenticated
  requests; `Principal` is the verified identity it binds to the request scope.
- `AuthConfigurationError` and `AuthDiscoveryError` are the startup failures.

The RFC 9728 protected-resource-metadata route lives in `ps_service.auth.protected_resource` and
is registered directly by `ps_service.main.create_app`, so it is not re-exported here.
"""

from __future__ import annotations

from ps_service.auth.errors import AuthConfigurationError, AuthDiscoveryError
from ps_service.auth.middleware import RestAuthMiddleware
from ps_service.auth.models import AuthContext, Principal
from ps_service.auth.startup import resolve_auth_context
from ps_service.auth.verifier import PsTokenVerifier

__all__ = [
    "AuthConfigurationError",
    "AuthContext",
    "AuthDiscoveryError",
    "Principal",
    "PsTokenVerifier",
    "RestAuthMiddleware",
    "resolve_auth_context",
]
