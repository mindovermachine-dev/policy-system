"""Domain-specific exception types for `ps_service.auth`.

A missing-configuration failure and an OIDC-discovery failure are unrelated failure modes, so each
has its own type rather than sharing a base hierarchy.
"""

from __future__ import annotations


class AuthConfigurationError(Exception):
    """`PS_AUTH_ISSUER`/`PS_AUTH_AUDIENCE` are missing and the local-test bypass is inactive.

    Raised by `ps_service.auth.startup.resolve_auth_context` at process startup. The message names
    exactly which variable(s) are unset and offers `PS_SERVICE_LOCAL_TEST_BYPASS` as the
    local-only alternative. The caller should set the variables (or the bypass, for local
    evaluation) and restart; the service does not start without them.
    """


class AuthDiscoveryError(Exception):
    """OIDC discovery against the configured issuer failed, or its response is unusable.

    Raised by `ps_service.auth.startup.resolve_auth_context` when the discovery document fetch
    fails, `jwks_uri` is absent from the response, or the issuer advertises no asymmetric
    algorithm this component recognises. The message names the issuer, never the raw underlying
    transport or parse error (which is chained as `__cause__` for fetch failures). The caller
    should verify the issuer URL and its reachability, then restart.
    """
