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
from ps_service.ingestion.adapters.errors import CellarFetchError

_RESOURCE_URL = "https://publications.europa.eu/resource/celex/{celex}"
_CONNECTIVITY_URL = "https://publications.europa.eu/"
_HEADERS = {
    "Accept": "application/xhtml+xml",
    "Accept-Language": "eng",
    "User-Agent": "ps-service-ingestion-cellar-eli/0.1 (+https://github.com/)",
}
_TIMEOUT_SECONDS = 30.0


class _FetchResponse(Protocol):
    """The minimal response shape `fetch_xhtml` needs — a context manager
    whose `read()` yields the body bytes. Deliberately narrower than
    `typing.IO[bytes]` (a nominal ABC with a much larger surface) so a
    lightweight test double can satisfy it structurally, matching what
    `urllib.request.urlopen` actually returns (`http.client.HTTPResponse`,
    which has exactly this shape)."""

    def read(self) -> bytes: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *exc_info: object) -> None: ...


class CellarTransport(Protocol):
    """The DI seam `fetch_xhtml` calls through (L2: business logic must not
    construct its own infrastructure clients inline). Matches
    `urllib.request.urlopen`'s call shape exactly, so the real `urlopen`
    can be the default transport with no adapter/wrapper needed, while a
    test can still substitute a mocked transport without monkeypatching
    `urllib` itself.
    """

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> _FetchResponse: ...


def fetch_xhtml(celex: str, *, transport: CellarTransport = urllib.request.urlopen) -> bytes:
    """Fetch a regulation's/directive's full XHTML text+structure from
    Cellar by CELEX number. Not specific to any single CELEX value — same
    call for CRA (32024R2847) and NIS2 (32022L2555). `transport` defaults
    to the real `urllib.request.urlopen` but is call-site injectable (L2
    DI) so unit tests can substitute a mocked transport.
    """
    url = _RESOURCE_URL.format(celex=celex)
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with transport(request, timeout=_TIMEOUT_SECONDS) as response:
            body = response.read()
    except Exception as exc:
        mark_unhealthy(CELLAR_ELI, error=exc)
        raise CellarFetchError(f"Cellar/ELI fetch failed for CELEX {celex!r} ({url}): {exc}") from exc
    mark_healthy(CELLAR_ELI)
    return body


def check_connectivity(*, transport: CellarTransport = urllib.request.urlopen) -> None:
    """Confirm the Cellar/ELI host itself is reachable, independent of any
    specific CELEX identifier — the cheapest real round-trip available,
    mirroring `falkordb_client.check_connectivity`'s pattern. An HTTP error
    status (`urllib.error.HTTPError`, e.g. a 404 on the bare domain) still
    means the host responded, so it counts as reachable; only a genuine
    network-level failure (DNS, connection refused, timeout — every other
    `URLError`/`OSError`) counts as unreachable.
    """
    request = urllib.request.Request(_CONNECTIVITY_URL, headers=_HEADERS)
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
