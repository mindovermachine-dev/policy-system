"""Value types shared by every `ps_service.auth` surface (REST middleware, MCP wiring).

Both are frozen dataclasses: once resolved, neither the process-lifetime auth configuration
(`AuthContext`) nor a per-request identity (`Principal`) may be mutated.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuthContext:
    """The fully-resolved, process-lifetime OIDC configuration for this PS Service instance.

    Built once by `ps_service.auth.startup.resolve_auth_context` and shared by the REST
    middleware and the MCP `token_verifier=` wiring -- never rebuilt per request.

    `scopes` is informational only (surfaced on the RFC 9728 protected-resource-metadata
    document) and is never enforced as authorization; only the audience is checked.

    Attributes:
        issuer: The OIDC issuer URL tokens must carry in `iss`.
        audience: The audience tokens must carry in `aud`.
        cli_client_id: The public OAuth client id advertised to the command-line client, or
            `None` when none is configured.
        scopes: Scopes advertised in the metadata document; may be empty.
        jwks_uri: URL of the issuer's signing-key set.
        allowed_algorithms: Asymmetric JWS algorithms a token's header may name; never empty.
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

    Both values are read from the verified token's claims, never from anything client-supplied
    outside the token.

    Attributes:
        sub: The token's `sub` claim, the caller's stable identifier at the issuer.
        iss: The token's `iss` claim, the issuer that vouches for `sub`.
    """

    sub: str
    iss: str
