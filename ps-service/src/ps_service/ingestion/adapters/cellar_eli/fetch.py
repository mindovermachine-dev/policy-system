"""Live fetch against Cellar/ELI: the HTTP calls this adapter makes.

The HTTP transport is injected, not called inline, so tests can substitute it.

Behaviour of the service, as the code relies on it:

- `https://publications.europa.eu/resource/celex/{CELEX}` (Cellar's machine-access resource
  endpoint) needs no auth and content-negotiates the same way for a regulation and a directive,
  so one code path serves every CELEX with no per-regulation branching.
- `Accept: application/xhtml+xml` with `Accept-Language: eng` returns the Official Journal's own
  XHTML rendering: a well-formed structural tree (`eli-container`, `eli-subdivision` and
  `eli-title` classes).
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Protocol, Self

from ps_service.dependency_health import CELLAR_ELI, mark_healthy, mark_unhealthy
from ps_service.ingestion.adapters.errors import CellarFetchError, CellarNotFoundError

_RESOURCE_URL = "https://publications.europa.eu/resource/celex/{celex}"
_CONNECTIVITY_URL = "https://publications.europa.eu/"
_USER_AGENT = "ps-service-ingestion-cellar-eli/0.1 (+https://github.com/)"
_XHTML_HEADERS = {
    "Accept": "application/xhtml+xml",
    "Accept-Language": "eng",
    "User-Agent": _USER_AGENT,
}
# No `Accept-Language` key: sending it with this `Accept` profile collapses the response to an
# empty per-language stub without the `cdm:date_*` / `cdm:resource_legal_id_celex` predicates
# the RDF path needs. `notice=non-inferred` is the leanest profile that returns the asserted
# (not reasoner-derived) triples.
_RDF_HEADERS = {
    "Accept": "application/rdf+xml;notice=non-inferred",
    "User-Agent": _USER_AGENT,
}
_TIMEOUT_SECONDS = 30.0
_HTTP_NOT_FOUND = 404


class _FetchResponse(Protocol):
    """The minimal response shape the fetch functions need.

    A context manager whose `read()` yields the body bytes. It is narrower than
    `typing.IO[bytes]` so a lightweight test double satisfies it structurally, and it matches what
    `urllib.request.urlopen` returns (`http.client.HTTPResponse`).
    """

    def read(self) -> bytes: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *exc_info: object) -> None: ...  # noqa: V107 - fixed context-manager exit signature


class CellarTransport(Protocol):
    """The injection seam the fetch functions call through.

    Matches `urllib.request.urlopen`'s call shape, so `urlopen` is the default transport and a
    test can substitute a fake without patching `urllib`.
    """

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> _FetchResponse:
        """Perform the HTTP round-trip for `request`.

        Args:
            request: The prepared request.
            timeout: Timeout in seconds for the round-trip.

        Returns:
            The response, usable as a context manager.
        """
        ...


def _fetch(celex: str, *, headers: dict[str, str], transport: CellarTransport) -> bytes:
    """Fetch a CELEX's Cellar/ELI resource with the given request `headers`.

    Shared by `fetch_xhtml` and `fetch_rdf`, which differ only in `headers`. Builds the URL,
    refuses a non-https URL, performs the request, and records Cellar/ELI health: success marks
    it healthy, a 404 leaves it unchanged, any other failure marks it unhealthy.

    Raises:
        CellarNotFoundError: The service answered 404 for the CELEX.
        CellarFetchError: The URL is not https, or the request failed for any other reason.
    """
    url = _RESOURCE_URL.format(celex=celex)
    if not url.startswith("https://"):
        raise CellarFetchError(f"refusing non-https Cellar/ELI URL for CELEX {celex!r}: {url}")
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 -- https scheme enforced by the guard on the line above
    try:
        with transport(request, timeout=_TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == _HTTP_NOT_FOUND:
            # The host responded, so this is not an outage and must not mark the dependency
            # unhealthy.
            raise CellarNotFoundError(
                f"CELEX {celex!r} was not found on Cellar/ELI ({url})"
            ) from exc
        mark_unhealthy(CELLAR_ELI, error=exc)
        raise CellarFetchError(
            f"Cellar/ELI fetch failed for CELEX {celex!r} ({url}): {exc}"
        ) from exc
    except Exception as exc:
        mark_unhealthy(CELLAR_ELI, error=exc)
        raise CellarFetchError(
            f"Cellar/ELI fetch failed for CELEX {celex!r} ({url}): {exc}"
        ) from exc
    mark_healthy(CELLAR_ELI)
    return body


def fetch_xhtml(celex: str, *, transport: CellarTransport = urllib.request.urlopen) -> bytes:
    """Fetch a regulation's or directive's full XHTML text and structure from Cellar.

    Args:
        celex: CELEX number of the instrument (for example `32024R2847`); the same call serves
            any CELEX.
        transport: HTTP transport; defaults to `urllib.request.urlopen`.

    Returns:
        The response body: the Official Journal XHTML document as bytes.

    Side Effects:
        One HTTPS GET to Cellar/ELI, and a Cellar/ELI health update.

    Raises:
        CellarNotFoundError: The service answered 404 for the CELEX.
        CellarFetchError: The request failed for any other reason.
    """
    return _fetch(celex, headers=_XHTML_HEADERS, transport=transport)


def fetch_rdf(celex: str, *, transport: CellarTransport = urllib.request.urlopen) -> bytes:
    """Fetch a regulation's or directive's structured RDF/XML metadata from Cellar.

    Uses the same endpoint as `fetch_xhtml`, content-negotiated for
    `application/rdf+xml;notice=non-inferred`, the profile whose `cdm:date_*` predicates
    `metadata.extract_metadata` parses for the effective date.

    Args:
        celex: CELEX number of the instrument.
        transport: HTTP transport; defaults to `urllib.request.urlopen`.

    Returns:
        The response body: the RDF/XML document as bytes.

    Side Effects:
        One HTTPS GET to Cellar/ELI, and a Cellar/ELI health update.

    Raises:
        CellarNotFoundError: The service answered 404 for the CELEX.
        CellarFetchError: The request failed for any other reason.
    """
    return _fetch(celex, headers=_RDF_HEADERS, transport=transport)


def check_connectivity(*, transport: CellarTransport = urllib.request.urlopen) -> None:
    """Confirm the Cellar/ELI host itself is reachable, independent of any CELEX.

    An HTTP error status (`urllib.error.HTTPError`, for example 404 on the bare domain) means the
    host responded, so it counts as reachable. Any other failure (DNS, connection refused,
    timeout) counts as unreachable.

    Args:
        transport: HTTP transport; defaults to `urllib.request.urlopen`.

    Side Effects:
        One HTTPS GET to the Cellar/ELI host, and a Cellar/ELI health update.

    Raises:
        CellarFetchError: The host could not be reached.
    """
    request = urllib.request.Request(_CONNECTIVITY_URL, headers=_XHTML_HEADERS)
    try:
        with transport(request, timeout=_TIMEOUT_SECONDS) as response:
            response.read()
    except urllib.error.HTTPError:
        pass
    except Exception as exc:
        mark_unhealthy(CELLAR_ELI, error=exc)
        raise CellarFetchError(
            f"Cellar/ELI connectivity check failed ({_CONNECTIVITY_URL}): {exc}"
        ) from exc
    mark_healthy(CELLAR_ELI)
