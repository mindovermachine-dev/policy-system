"""Injectable-transport HTTP GET mechanics for the Curated Source component.

Mirrors `ps_service.ingestion.adapters.cellar_eli.fetch`'s `CellarTransport`
Protocol / `_fetch` shape exactly (timeout, request construction, error
handling) -- the proven, already-reviewed pattern this codebase uses for an
injectable HTTP GET (L2 DI: business logic must not construct its own
infrastructure clients inline).

Deliberately carries no `dependency_health` integration: PLAN.md §7 flags
this explicitly as out of scope for this issue (no AC requires a curated-
source `/ready` probe; adding one would be scope creep).
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Protocol, Self

from ps_service.curated_source.errors import CuratedSourceFetchError

_TIMEOUT_SECONDS = 30.0
_USER_AGENT = "ps-service-curated-source/0.1 (+https://github.com/)"


class _FetchResponse(Protocol):
    """The minimal response shape `fetch_bytes` needs.

    A context manager whose `read()` yields the body bytes -- mirrors
    `cellar_eli.fetch._FetchResponse` exactly, matching what
    `urllib.request.urlopen` actually returns.
    """

    def read(self) -> bytes: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *exc_info: object) -> None: ...


class CuratedSourceTransport(Protocol):
    """The DI seam `fetch_bytes` calls through.

    Matches `urllib.request.urlopen`'s call shape exactly, so the real
    `urlopen` can be the default transport with no adapter/wrapper needed,
    while a test can substitute a fake transport without monkeypatching
    `urllib` itself.
    """

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> _FetchResponse:
        """Perform the HTTP round-trip for `request`, returning the response."""
        ...


def fetch_bytes(url: str, *, transport: CuratedSourceTransport = urllib.request.urlopen) -> bytes:
    """GET `url`'s raw response body, naming the source on any failure (AC-BI-006).

    `url` is trusted to already be http(s)-validated
    (`source_url.validate_source_url` runs before this is ever reached, both
    at startup config-load time and in `set-catalog-source`) -- this function
    performs the round-trip only; it does not re-validate the scheme.

    Args:
        url: The exact URL to GET (e.g. `{base_url}/catalog.json`).
        transport: The HTTP transport to use -- defaults to the real
            `urllib.request.urlopen`, but is call-site injectable (L2 DI) so
            tests never reach real network.

    Returns:
        The response body bytes.

    Raises:
        CuratedSourceFetchError: The request failed for any reason (DNS,
            connection refused, timeout, a non-2xx HTTP status) -- the
            message always names `url`.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})  # noqa: S310 -- caller validates the http(s) scheme before this is ever reached
    try:
        with transport(request, timeout=_TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise CuratedSourceFetchError(
            f"fetch failed for curated source {url!r}: HTTP {exc.code} {exc.reason}"
        ) from exc
    except Exception as exc:
        raise CuratedSourceFetchError(f"fetch failed for curated source {url!r}: {exc}") from exc
