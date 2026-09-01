"""Live fetch against Cellar/ELI — the one HTTP call this module makes.

Ports `spikes/cellar1/cellar_client.py`'s fetch logic, DI-friendly per L2
(the HTTP transport is injected, not called inline).

Verified directly against the real service (see `spikes/cellar1/
LEARNINGS.md`):

- `https://publications.europa.eu/resource/celex/{CELEX}` (Cellar's
  machine-access resource endpoint) needs no auth and content-negotiates
  the same way for a regulation (CRA, 32024R2847) and a directive (NIS2,
  32022L2555) — confirmed both, one code path, no per-regulation
  branching.
- `Accept: application/xhtml+xml` + `Accept-Language: eng` returns the
  Official Journal's own XHTML rendering: a real, well-formed structural
  tree (`eli-container`/`eli-subdivision`/`eli-title` classes).
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
# No `Accept-Language` key here -- deliberate, load-bearing (PLAN_REVISED.md
# §1): live-verified against real Cellar/ELI data that sending
# `Accept-Language` at any value, together with this `Accept` profile,
# collapses the response to an empty per-language stub with none of the
# structured `cdm:date_*`/`cdm:resource_legal_id_celex` predicates the RDF
# path needs. `notice=non-inferred` is the leanest profile confirmed to
# return the real, asserted (non-reasoner-derived) triples.
_RDF_HEADERS = {
    "Accept": "application/rdf+xml;notice=non-inferred",
    "User-Agent": _USER_AGENT,
}
_TIMEOUT_SECONDS = 30.0
_HTTP_NOT_FOUND = 404


class _FetchResponse(Protocol):
    """The minimal response shape `fetch_xhtml` needs.

    A context manager whose `read()` yields the body bytes. Deliberately
    narrower than `typing.IO[bytes]` (a nominal ABC with a much larger
    surface) so a lightweight test double can satisfy it structurally,
    matching what `urllib.request.urlopen` actually returns
    (`http.client.HTTPResponse`, which has exactly this shape).
    """

    def read(self) -> bytes: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *exc_info: object) -> None: ...


class CellarTransport(Protocol):
    """The DI seam `fetch_xhtml` calls through.

    L2: business logic must not construct its own infrastructure clients
    inline. Matches `urllib.request.urlopen`'s call shape exactly, so the
    real `urlopen` can be the default transport with no adapter/wrapper
    needed, while a test can still substitute a mocked transport without
    monkeypatching `urllib` itself.
    """

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> _FetchResponse:
        """Perform the HTTP round-trip for `request`, returning the response."""
        ...


def _fetch(celex: str, *, headers: dict[str, str], transport: CellarTransport) -> bytes:
    """Shared HTTP mechanics for fetching a CELEX's Cellar/ELI resource.

    Identical for every `Accept` profile: URL construction, the https-only
    guard, request building, error handling (including the 404 special
    case), and `dependency_health` reporting. `fetch_xhtml`/`fetch_rdf`
    differ only in the `headers` they pass through.
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
            # The host responded -- the CELEX genuinely doesn't resolve. Not a
            # Cellar/ELI outage, so it must not mark the dependency unhealthy.
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
    """Fetch a regulation's/directive's full XHTML text and structure from Cellar.

    Keyed by CELEX number; not specific to any single CELEX value — same
    call for CRA (32024R2847) and NIS2 (32022L2555). `transport` defaults
    to the real `urllib.request.urlopen` but is call-site injectable (L2
    DI) so unit tests can substitute a mocked transport.
    """
    return _fetch(celex, headers=_XHTML_HEADERS, transport=transport)


def fetch_rdf(celex: str, *, transport: CellarTransport = urllib.request.urlopen) -> bytes:
    """Fetch a regulation's/directive's structured RDF/XML metadata from Cellar.

    Same CELEX-keyed resource endpoint as `fetch_xhtml`, but content-
    negotiated for `application/rdf+xml;notice=non-inferred` instead of
    XHTML — the profile `metadata.py`'s effective-date extraction parses
    for structured `cdm:date_*` predicates. `transport` defaults to the
    real `urllib.request.urlopen` but is call-site injectable (L2 DI) so
    unit tests can substitute a mocked transport.
    """
    return _fetch(celex, headers=_RDF_HEADERS, transport=transport)


def check_connectivity(*, transport: CellarTransport = urllib.request.urlopen) -> None:
    """Confirm the Cellar/ELI host itself is reachable.

    Independent of any specific CELEX identifier — the cheapest real
    round-trip available, mirroring `falkordb_client.check_connectivity`'s
    pattern. An HTTP error status (`urllib.error.HTTPError`, e.g. a 404 on
    the bare domain) still means the host responded, so it counts as
    reachable; only a genuine network-level failure (DNS, connection
    refused, timeout — every other `URLError`/`OSError`) counts as
    unreachable.
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
