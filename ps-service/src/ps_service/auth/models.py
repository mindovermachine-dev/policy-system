"""Value types shared by every `ps_service.auth` surface (REST middleware, MCP wiring).

Both are frozen dataclasses: once resolved, neither the process-lifetime
auth configuration (`AuthContext`) nor a per-request identity (`Principal`)
may be mutated.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuthContext:
    """The fully-resolved, process-lifetime OIDC configuration for this PS Service instance.

    Built once by `ps_service.auth.startup.resolve_auth_context` (issue #58,
    AC-BI-001) and shared by both the REST middleware and the MCP
    `token_verifier=` wiring -- never rebuilt per request.

    `scopes` is informational only (surfaced on the RFC 9728
    protected-resource-metadata document) and is never enforced as
    authorization -- see TASK.md's "Out of scope: role/scope-based authz
    (audience check only)".
    """

    issuer: str
    audience: str
    cli_client_id: str | None
    scopes: tuple[str, ...]
    jwks_uri: str
    allowed_algorithms: frozenset[str]


@dataclass(frozen=True)
class Principal:
    """The verified identity of the caller who presented a valid bearer token.

    `sub`/`iss` are read straight from the verified token's claims (issue
    #58, AC-BI-005/AC-BI-007) -- never derived from anything client-supplied
    outside the token itself.
    """

    sub: str
    iss: str
