"""Process-startup resolution of OIDC auth configuration (issue #58).

`resolve_auth_context` is called synchronously inside `create_app(config)`'s
own body (see `ps_service.main`), not inside the async `lifespan()` closure
-- `RestAuthMiddleware` (a later slice) is added via `app.add_middleware`
at `create_app` build time and must have a concrete `AuthContext` (or
`None`, for the local-test bypass) the moment the app exists, since a pure
ASGI middleware runs per-request regardless of whether `lifespan` was ever
entered.
"""

from __future__ import annotations

import urllib.request
from typing import TYPE_CHECKING, cast

from ps_service.auth.discovery import (
    DiscoveryTransport,
    compute_allowed_algorithms,
    fetch_discovery_document,
)
from ps_service.auth.errors import AuthConfigurationError, AuthDiscoveryError
from ps_service.auth.models import AuthContext

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig

_LOCAL_TEST_BYPASS_VAR = "PS_SERVICE_LOCAL_TEST_BYPASS"


def _format_missing_auth_config_message(missing: list[str]) -> str:
    """Build AC-BI-002's exact message shape: name the missing var(s) and the bypass alternative."""
    joined = " and ".join(missing)
    verb = "is" if len(missing) == 1 else "are"
    return (
        f"{joined} {verb} unset; set both PS_AUTH_ISSUER and PS_AUTH_AUDIENCE, "
        f"or set {_LOCAL_TEST_BYPASS_VAR}=true for local-only evaluation."
    )


def resolve_auth_context(
    config: ServiceConfig, *, transport: DiscoveryTransport = urllib.request.urlopen
) -> AuthContext | None:
    """Resolve `config` into an `AuthContext`, or raise, or return `None` for the bypass.

    - Local-test bypass (issue #67) active: returns `None` unconditionally
      -- no auth configured, matching #67's existing contract, regardless
      of whether `PS_AUTH_ISSUER`/`PS_AUTH_AUDIENCE` happen to be set. Never
      a "vacuous" `AuthContext`.
    - Bypass inactive and either `PS_AUTH_ISSUER` or `PS_AUTH_AUDIENCE` is
      unset: raises `AuthConfigurationError` naming exactly which
      variable(s) are missing and `PS_SERVICE_LOCAL_TEST_BYPASS` as the
      local-only alternative (issue #58, AC-BI-002).
    - Bypass inactive and both are set: fetches
      `<issuer>/.well-known/openid-configuration` (AC-BI-001) via
      `fetch_discovery_document`, whose `transport` is injectable here (DI
      seam, defaults to the real `urllib.request.urlopen`) purely so tests
      can substitute a fake transport -- `ps_service.main.create_app` never
      passes a non-default `transport`. Raises `AuthDiscoveryError` naming
      the issuer if the fetch fails, `jwks_uri` is absent, or the issuer
      advertises no algorithm this component's asymmetric allow-list
      recognizes (AC-BI-009's discovery-time half, computed once here --
      per-token enforcement of the allow-list is a later slice's job).
    """
    if config.is_local_test_bypass_active:
        return None

    if config.auth_issuer is None or config.auth_audience is None:
        missing = [
            name
            for name, value in (
                ("PS_AUTH_ISSUER", config.auth_issuer),
                ("PS_AUTH_AUDIENCE", config.auth_audience),
            )
            if value is None
        ]
        raise AuthConfigurationError(_format_missing_auth_config_message(missing))

    issuer = config.auth_issuer
    try:
        document = fetch_discovery_document(issuer, transport=transport)
    except Exception as exc:
        raise AuthDiscoveryError(
            f"OIDC discovery failed for issuer {issuer!r}. "
            "Verify the issuer URL is reachable and correctly configured."
        ) from exc

    jwks_uri: object = document.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise AuthDiscoveryError(f"OIDC discovery document for issuer {issuer!r} has no jwks_uri")

    alg_values_raw: object = document.get("id_token_signing_alg_values_supported")
    alg_values: list[str] = []
    if isinstance(alg_values_raw, list):
        # `isinstance(alg_values_raw, list)` narrows `object` to `list[Unknown]` (no runtime
        # type-arg info) rather than `list[object]`; this cast restates the element type the
        # `isinstance(value, str)` filter below already enforces at runtime.
        untyped_alg_values = cast("list[object]", alg_values_raw)
        alg_values = [value for value in untyped_alg_values if isinstance(value, str)]
    allowed_algorithms = compute_allowed_algorithms(alg_values)
    if not allowed_algorithms:
        raise AuthDiscoveryError(
            f"OIDC discovery document for issuer {issuer!r} advertises no supported "
            "asymmetric signing algorithm"
        )

    return AuthContext(
        issuer=issuer,
        audience=config.auth_audience,
        cli_client_id=config.auth_cli_client_id,
        scopes=config.auth_scopes,
        jwks_uri=jwks_uri,
        allowed_algorithms=allowed_algorithms,
    )
