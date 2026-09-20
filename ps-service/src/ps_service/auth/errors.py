"""Domain-specific exception types for `ps_service.auth`.

One exception type per distinct failure boundary this component owns (L2
"one exception type per distinct failure boundary"): a fail-closed
missing-configuration boundary and an OIDC-discovery-fetch boundary are
unrelated failure modes, so they get separate types rather than a shared
base hierarchy.
"""

from __future__ import annotations


class AuthConfigurationError(Exception):
    """`PS_AUTH_ISSUER`/`PS_AUTH_AUDIENCE` are missing and the local-test bypass is inactive.

    Raised by `ps_service.auth.startup.resolve_auth_context` (issue #58,
    AC-BI-002). The message names exactly which variable(s) are unset and
    `PS_SERVICE_LOCAL_TEST_BYPASS` as the local-only alternative -- never a
    generic "auth misconfigured" message.
    """


class AuthDiscoveryError(Exception):
    """OIDC discovery against the configured issuer failed, or its response is unusable.

    Raised by `ps_service.auth.startup.resolve_auth_context` (issue #58,
    AC-BI-001) when the discovery document fetch fails, `jwks_uri` is
    absent from the response, or the issuer advertises no algorithm this
    component's asymmetric allow-list recognizes. The message names the
    issuer, never the raw underlying transport/parse error.
    """
