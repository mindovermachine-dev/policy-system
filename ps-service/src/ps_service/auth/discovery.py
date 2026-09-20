"""OIDC discovery-document fetch and asymmetric-algorithm allow-list math (issue #58).

`fetch_discovery_document` is the one HTTP call this module makes -- mirrors
`ps_service.ingestion.adapters.cellar_eli.fetch`'s `CellarTransport`
injection pattern exactly (L2 DI): the real `urllib.request.urlopen` is the
default transport, call-site injectable so tests substitute a fake without
monkeypatching `urllib` itself. Unlike `PyJWKClient` (a PyJWT SDK class with
no `transport=` seam -- see PLAN.md §0.2), this fetch is entirely this
component's own code, so it gets the same seam `fetch.py` already
established.

`compute_allowed_algorithms` is AC-BI-009's discovery-time half: the
asymmetric-algorithm allow-list is computed once here, from the issuer's
*advertised* `id_token_signing_alg_values_supported`, at startup -- never
derived from a token's own `alg` header at verification time (PyJWT's own
documented warning, `jwt/api_jwt.py`: "Do not compute the `algorithms`
parameter based on the `alg` from the token itself").
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Final, Protocol, Self, cast

_TIMEOUT_SECONDS = 30.0
_DISCOVERY_PATH = "/.well-known/openid-configuration"

# PyJWT's asymmetric algorithm family (`jwt/algorithms.py`), excluding the
# symmetric HS256/HS384/HS512/none family entirely, and deliberately
# excluding PyJWT's `ES521` legacy alias: `algorithms.py` registers it as
# the exact same `ECAlgorithm(ECAlgorithm.SHA512, SECP521R1)` object as
# `ES512` -- no distinct cryptographic capability -- and `ES521` is not an
# IANA-registered JOSE `alg` value, so no real IdP advertises it (CHANGES.md
# item 4, justified skip: adding it would be dead, untestable code).
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

    A context manager whose `read()` yields the body bytes -- deliberately
    narrower than `typing.IO[bytes]`, matching what `urllib.request.urlopen`
    actually returns (`http.client.HTTPResponse`), mirroring
    `cellar_eli.fetch._FetchResponse` exactly.
    """

    def read(self) -> bytes: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *exc_info: object) -> None: ...


class DiscoveryTransport(Protocol):
    """The DI seam `fetch_discovery_document` calls through.

    Matches `urllib.request.urlopen`'s call shape exactly (and
    `cellar_eli.fetch.CellarTransport`'s identical shape), so the real
    `urlopen` can be the default transport with no adapter needed, while a
    test substitutes a fake transport instead of monkeypatching `urllib`.
    """

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> _DiscoveryResponse:
        """Perform the HTTP round-trip for `request`, returning the response."""
        ...


def fetch_discovery_document(
    issuer: str, *, transport: DiscoveryTransport = urllib.request.urlopen
) -> dict[str, Any]:
    """Fetch and parse `<issuer>/.well-known/openid-configuration` (AC-BI-001).

    Raises the raw underlying error on any transport failure (connection
    error, non-2xx status -- `urlopen` itself raises `urllib.error.HTTPError`
    for those) or on a malformed/non-object JSON body (`json.JSONDecodeError`,
    or `TypeError` when the parsed body is valid JSON but not an object).
    The caller, `ps_service.auth.startup.resolve_auth_context`, is
    responsible for translating any exception raised here into
    `AuthDiscoveryError` naming the issuer (AC-BI-001) -- this function does
    not shape that error itself, so the shaping decision is not duplicated.
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
    """Compute AC-BI-009's asymmetric-algorithm allow-list from a discovery document.

    Intersects the issuer's advertised `id_token_signing_alg_values_supported`
    with PyJWT's asymmetric algorithm family -- excludes symmetric (`HS256`/
    `HS384`/`HS512`) and `none` even if an issuer were to (incorrectly)
    advertise one. An issuer advertising no recognized asymmetric algorithm
    yields an empty set; `resolve_auth_context` treats that as a discovery
    failure (AC-BI-001), not a silently-accepted zero-algorithm `AuthContext`.
    """
    return frozenset(id_token_signing_alg_values_supported) & _ASYMMETRIC_ALGORITHMS
