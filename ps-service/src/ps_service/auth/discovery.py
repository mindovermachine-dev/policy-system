"""OIDC discovery-document fetch and asymmetric-algorithm allow-list computation.

`fetch_discovery_document` is the one HTTP call this module makes. The transport is injectable
(real `urllib.request.urlopen` by default) so tests substitute a fake without monkeypatching
`urllib`.

`compute_allowed_algorithms` computes the asymmetric signing-algorithm allow-list once, at
startup, from the issuer's advertised `id_token_signing_alg_values_supported`. It is never derived
from a token's own `alg` header at verification time, because trusting the token to name its own
algorithm lets an attacker pick one the issuer never uses.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Final, Protocol, Self, cast

_TIMEOUT_SECONDS = 30.0
_DISCOVERY_PATH = "/.well-known/openid-configuration"

# PyJWT's asymmetric algorithm family, excluding the symmetric HS256/HS384/HS512/none family
# entirely, and deliberately excluding PyJWT's `ES521` legacy alias: it is the same object as
# `ES512` (no distinct capability) and is not an IANA-registered JOSE `alg` value, so no real
# issuer advertises it.
_ASYMMETRIC_ALGORITHMS: Final = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES256K",
        "ES384",
        "ES512",
        "EdDSA",
    }
)


class _DiscoveryResponse(Protocol):
    """The minimal response shape `fetch_discovery_document` needs.

    A context manager whose `read()` yields the body bytes -- deliberately narrower than
    `typing.IO[bytes]`, matching what `urllib.request.urlopen` actually returns.
    """

    def read(self) -> bytes:
        """Read the whole response body.

        Returns:
            The raw body bytes; empty bytes for an empty body.

        Raises:
            OSError: The connection failed while reading.

        Side Effects:
            Consumes the response stream.
        """
        ...

    def __enter__(self) -> Self:
        """Enter the response context.

        Returns:
            The response itself.

        Raises:
            Nothing: entering never fails.

        Side Effects:
            None.
        """
        ...

    def __exit__(self, *exc_info: object) -> None:
        """Leave the response context and release the connection.

        Args:
            *exc_info: The exception type, value and traceback in flight, or `None` values.

        Returns:
            `None`; exceptions in flight are not suppressed.

        Raises:
            Nothing: closing never fails.

        Side Effects:
            Closes the underlying connection.
        """
        ...


class DiscoveryTransport(Protocol):
    """The injection seam `fetch_discovery_document` calls through.

    Matches `urllib.request.urlopen`'s call shape, so the real `urlopen` is the default transport
    with no adapter, while a test substitutes a fake transport instead of monkeypatching `urllib`.
    """

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> _DiscoveryResponse:
        """Perform the HTTP round-trip for `request`.

        Args:
            request: The prepared HTTP request (GET of the discovery URL).
            timeout: Seconds to wait for the connection and each read; positive.

        Returns:
            The open response, to be used as a context manager.

        Raises:
            urllib.error.HTTPError: The server answered with a non-2xx status.
            urllib.error.URLError: The connection could not be made.
            TimeoutError: The server did not answer within `timeout`.

        Side Effects:
            One outbound network request.
        """
        ...


def fetch_discovery_document(
    issuer: str, *, transport: DiscoveryTransport = urllib.request.urlopen
) -> dict[str, Any]:
    """Fetch and parse `<issuer>/.well-known/openid-configuration`.

    The raw underlying errors are deliberately not translated here; the startup resolver
    (`ps_service.auth.startup.resolve_auth_context`) turns any of them into an
    `AuthDiscoveryError` that names the issuer, so that decision lives in one place.

    Args:
        issuer: The issuer base URL, operator-configured; a trailing slash is ignored.
        transport: Callable performing the HTTP round-trip; defaults to the real
            `urllib.request.urlopen`. Called with a 30-second timeout.

    Returns:
        The parsed discovery document, a JSON object keyed by string.

    Raises:
        urllib.error.HTTPError: The issuer answered with a non-2xx status.
        urllib.error.URLError: The issuer could not be reached.
        TimeoutError: The issuer did not answer within the timeout.
        json.JSONDecodeError: The body is not valid JSON.
        TypeError: The body is valid JSON but not an object.

    Side Effects:
        One outbound HTTP GET to the issuer.
    """
    url = f"{issuer.rstrip('/')}{_DISCOVERY_PATH}"
    request = urllib.request.Request(url)  # noqa: S310 -- issuer is operator-configured, not user input
    with transport(request, timeout=_TIMEOUT_SECONDS) as response:
        body = response.read()
    parsed: object = json.loads(body)
    if not isinstance(parsed, dict):
        raise TypeError(
            f"OIDC discovery document body is not a JSON object (got {type(parsed).__name__})"
        )
    # JSON object keys are always `str` once `json.loads` succeeds; the `isinstance` check
    # above only narrows to `dict[Any, Any]`, so this cast restates that guaranteed fact for
    # the declared return type -- no behavior depends on it.
    return cast("dict[str, Any]", parsed)


def compute_allowed_algorithms(id_token_signing_alg_values_supported: list[str]) -> frozenset[str]:
    """Compute the asymmetric signing-algorithm allow-list from a discovery document.

    Intersects the issuer's advertised algorithms with PyJWT's asymmetric family, so symmetric
    (`HS256`/`HS384`/`HS512`) and `none` are excluded even if an issuer incorrectly advertises
    them.

    Args:
        id_token_signing_alg_values_supported: The `id_token_signing_alg_values_supported` list
            from the discovery document; may be empty.

    Returns:
        The advertised algorithms that are asymmetric and recognised. Empty when the issuer
        advertises none; `resolve_auth_context` treats an empty result as a discovery failure
        rather than building a context that accepts no algorithm.
    """
    return frozenset(id_token_signing_alg_values_supported) & _ASYMMETRIC_ALGORITHMS
