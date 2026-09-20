"""ps_service.auth -- package front door.

Shared OIDC bearer-token configuration/verification component used by both
the REST middleware and the MCP `token_verifier=` wiring (issue #58). As of
Slice 3, fail-closed startup configuration resolution (`resolve_auth_context`,
AC-BI-001/AC-BI-002), the shared `PsTokenVerifier` (AC-BI-003 signature/iss/
aud/exp/nbf validation), and `RestAuthMiddleware` (the REST-side 401 gate)
are implemented. The RFC 9728 protected-resource endpoint lands in a later
slice and is not yet re-exported here.
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
