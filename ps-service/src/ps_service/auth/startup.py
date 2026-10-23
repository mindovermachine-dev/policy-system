"""Process-startup resolution of OIDC auth configuration.

`resolve_auth_context` is called synchronously inside `create_app(config)`'s body (see
`ps_service.main`), not inside the async `lifespan()` closure: `RestAuthMiddleware` is added at
`create_app` build time and must have a concrete `AuthContext` (or `None`, for the local-test
bypass) the moment the app exists, since the middleware runs per request whether or not
`lifespan` was ever entered.
"""

from __future__ import annotations

import sys
import time
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
    from collections.abc import Callable
    from typing import Any

    from ps_service.config import ServiceConfig

_LOCAL_TEST_BYPASS_VAR = "PS_SERVICE_LOCAL_TEST_BYPASS"
_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 30.0


def _format_missing_auth_config_message(missing: list[str]) -> str:
    """Build the startup error message naming the missing variable(s) and the bypass alternative."""
    joined = " and ".join(missing)
    verb = "is" if len(missing) == 1 else "are"
    return (
        f"{joined} {verb} unset; set both PS_AUTH_ISSUER and PS_AUTH_AUDIENCE, "
        f"or set {_LOCAL_TEST_BYPASS_VAR}=true for local-only evaluation."
    )


def _report_retry(attempt: int, retry_in_seconds: float, error: Exception) -> None:
    """Write one line to stderr for a failed discovery attempt that will be retried.

    The log facade is not configured yet when startup resolves authentication, so this uses
    stderr directly. Only the error class is written, never the raw error text.
    """
    sys.stderr.write(
        f"component=auth action=oidc_discovery outcome=retrying attempt={attempt} "
        f"retry_in_seconds={retry_in_seconds:g} error_class={type(error).__name__}\n"
    )


def _fetch_with_retry(
    issuer: str,
    *,
    transport: DiscoveryTransport,
    budget_seconds: int,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> dict[str, Any]:
    """Fetch the discovery document, retrying failures with doubling backoff within a budget.

    Backoff starts at one second, doubles and is capped at thirty seconds; the last sleep is
    shortened so the total sleeping never exceeds `budget_seconds`. A budget of zero makes one
    attempt. The failure of the final attempt is chained on the raised error.

    Raises:
        AuthDiscoveryError: Every attempt within the budget failed; the message names the issuer.
    """
    started = monotonic()
    delay = _INITIAL_BACKOFF_SECONDS
    attempt = 1
    while True:
        try:
            return fetch_discovery_document(issuer, transport=transport)
        except Exception as exc:
            remaining = budget_seconds - (monotonic() - started)
            if remaining <= 0:
                raise AuthDiscoveryError(
                    f"OIDC discovery failed for issuer {issuer!r}. "
                    "Verify the issuer URL is reachable and correctly configured."
                ) from exc
            pause = min(delay, remaining)
            _report_retry(attempt, pause, exc)
            sleep(pause)
            delay = min(delay * 2, _MAX_BACKOFF_SECONDS)
            attempt += 1


def resolve_auth_context(
    config: ServiceConfig,
    *,
    transport: DiscoveryTransport = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> AuthContext | None:
    """Resolve `config` into the process-lifetime `AuthContext`, failing closed when it cannot.

    Business Rules:
        - Local-test bypass active: returns `None` regardless of whether the issuer and audience
          are set; never a vacuous `AuthContext`.
        - Bypass inactive and the issuer or audience is unset: raises, naming exactly which
          variable(s) are missing and the bypass as the local-only alternative.
        - Bypass inactive and both set: fetches the issuer's OIDC discovery document, requires a
          non-empty `jwks_uri`, and computes the asymmetric-algorithm allow-list from the
          advertised algorithms (non-string entries are ignored); an empty allow-list is a
          failure.
        - A failed fetch is retried with doubling backoff (1 s, capped at 30 s) until
          `auth_discovery_retry_budget_seconds` is spent, then fails closed; a budget of zero
          makes one attempt. Only the fetch is retried: a missing issuer or audience, a document
          without `jwks_uri` and a document without a supported algorithm fail at once.

    Args:
        config: The service configuration; reads `is_local_test_bypass_active`, `auth_issuer`,
            `auth_audience`, `auth_discovery_retry_budget_seconds`, `auth_cli_client_id` and
            `auth_scopes`.
        transport: HTTP transport for the discovery fetch; defaults to the real
            `urllib.request.urlopen` and is overridden only by tests.
        sleep: Pause between discovery attempts; defaults to `time.sleep`.
        monotonic: Clock that measures the retry budget; defaults to `time.monotonic`.

    Returns:
        The resolved `AuthContext`, or `None` when the local-test bypass is active (no
        authentication is configured).

    Raises:
        AuthConfigurationError: The bypass is inactive and the issuer or audience is unset.
        AuthDiscoveryError: The discovery fetch kept failing until the retry budget was spent
            (the last cause is chained), `jwks_uri` is missing or empty, or the issuer advertises
            no supported asymmetric algorithm. The message names the issuer.

    Side Effects:
        One or more outbound HTTP GETs to the issuer's discovery endpoint, with sleeps totalling
        at most the retry budget between them, when the bypass is inactive and configuration is
        complete; none otherwise. Each retried failure writes one line to stderr.
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
    document = _fetch_with_retry(
        issuer,
        transport=transport,
        budget_seconds=config.auth_discovery_retry_budget_seconds,
        sleep=sleep,
        monotonic=monotonic,
    )

    jwks_uri: object = document.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise AuthDiscoveryError(f"OIDC discovery document for issuer {issuer!r} has no jwks_uri")

    alg_values_raw: object = document.get("id_token_signing_alg_values_supported")
    alg_values: list[str] = []
    if isinstance(alg_values_raw, list):
        # `isinstance` narrows `object` to `list[Unknown]`; the cast restates the element type
        # that the `isinstance(value, str)` filter below enforces at runtime.
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


def require_public_url_configured(config: ServiceConfig) -> None:
    """Fail closed unless the service's public origin is configured or the bypass is active.

    Args:
        config: The service configuration; reads `is_local_test_bypass_active` and `public_url`.

    Returns:
        `None` when the bypass is active or `public_url` is set.

    Raises:
        AuthConfigurationError: The bypass is inactive and `public_url` is unset; the message
            names `PS_PUBLIC_URL` and the bypass as the local-only alternative.
    """
    if config.is_local_test_bypass_active or config.public_url is not None:
        return
    message = (
        "PS_PUBLIC_URL is unset; set it to the service's public origin (https://host[:port]), "
        f"or set {_LOCAL_TEST_BYPASS_VAR}=true for local-only evaluation."
    )
    raise AuthConfigurationError(message)
