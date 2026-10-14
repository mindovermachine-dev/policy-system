"""HTTP GET with an injectable transport for the Curated Source component.

The request construction, timeout and error handling follow the `CellarTransport` pattern of
`ps_service.ingestion.adapters.cellar_eli.fetch`: business logic must not construct its own
infrastructure clients inline, so the transport is passed in.

The module deliberately has no `dependency_health` integration: no readiness probe depends on
the curated source.
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

    A context manager whose `read()` yields the body bytes, which is what
    `urllib.request.urlopen` returns.
    """

    def read(self) -> bytes: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *exc_info: object) -> None: ...  # noqa: V107 - fixed context-manager exit signature


class CuratedSourceTransport(Protocol):
    """The dependency-injection seam `fetch_bytes` calls through.

    Matches the call shape of `urllib.request.urlopen`, so the real `urlopen` is the default
    transport without a wrapper, while a test can pass a fake without monkeypatching `urllib`.
    """

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> _FetchResponse:
        """Perform the HTTP round-trip for `request`.

        Args:
            request: The prepared GET request.
            timeout: Socket timeout in seconds.

        Returns:
            The response, usable as a context manager whose `read()` yields the body bytes.
        """
        ...


def fetch_bytes(url: str, *, transport: CuratedSourceTransport = urllib.request.urlopen) -> bytes:
    """GET `url` and return the raw response body, naming the source on any failure.

    `url` must already be validated as http(s) by `source_url.validate_source_url`, which runs
    at startup configuration load and in `set-catalog-source`; this function performs the
    round-trip only and does not re-check the scheme.

    Args:
        url: The exact URL to GET, for example `{base_url}/catalog.json`.
        transport: HTTP transport; defaults to the real `urllib.request.urlopen`. Injectable so
            tests never reach the network.

    Returns:
        The response body bytes; an empty body returns `b""`.

    Raises:
        CuratedSourceFetchError: The request failed for any reason (DNS, connection refused,
            timeout, a non-2xx HTTP status). The message always names `url`.

    Side Effects:
        One outbound HTTP GET with a fixed User-Agent and a 30 second timeout.
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
