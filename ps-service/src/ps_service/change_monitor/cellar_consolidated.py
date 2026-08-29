"""CELLAR SPARQL client: detect the consolidated expressions of a base act (AC-001).

`poll_for_amendments` holds a base-act CELEX (e.g. `32016R0679`) and needs to
know every *consolidated* expression CELLAR has published for it, newest last.
This module is the one place that talks to the CELLAR SPARQL endpoint. The
HTTP transport is injected (`SparqlTransport`), mirroring
`ps_service.ingestion.adapters.cellar_eli.fetch.CellarTransport` -- business
logic never constructs its own client inline (L2 DI), and a unit test
substitutes a fake without monkeypatching `urllib`.

Design (PLAN_REVIEWED.md §1.1, verified live):

- Predicate `cdm:act_consolidated_consolidates_resource_legal`; endpoint
  `https://publications.europa.eu/webapi/rdf/sparql`; HTTP `GET` with
  `query=<SPARQL>` + `format=application/sparql-results+json`; no auth; 30 s
  timeout; **https-only guard** on the constructed URL.
- The query runs **without `LIMIT`** -- the poll needs the full set for its
  date comparison. `ConsolidatedVersionInfo` sorts ascending and exposes
  `latest_celex` / `latest_consolidation_date`.
- The `-YYYYMMDD` consolidation suffix ("amendments incorporated up to") is
  zero-padded, so the CELEX strings sort lexically. The date is derived in
  Python (`date(int(s[11:15]), int(s[15:17]), int(s[17:19]))`) -- a SPARQL
  `xsd:date` BIND is not portable on this engine.
- `_consolidated_celex_prefix` validates the base CELEX and asserts it is in
  the legislation sector (`3`) before building the `0<base>-` FILTER prefix
  (PLAN_REVIEWED.md flaw 15) -- not a blind character replace. A
  consolidated-form CELEX (leading `0`, `-date` suffix) is rejected; the poll
  always holds a base-act CELEX.

## AC-001 traceability -- exact live query + one captured response

The SPARQL sent for base CELEX `32016R0679` (GDPR), verbatim:

    PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
    PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
    SELECT ?consolidatedCelex WHERE {
      ?base cdm:resource_legal_id_celex "32016R0679"^^xsd:string .
      ?consolidatedWork cdm:act_consolidated_consolidates_resource_legal ?base ;
                        cdm:resource_legal_id_celex ?consolidatedCelex .
      FILTER(STRSTARTS(STR(?consolidatedCelex), "02016R0679-"))
    }
    ORDER BY DESC(?consolidatedCelex)

The live endpoint's response (HTTP 200,
`Content-Type: application/sparql-results+json`), captured 2026-08-29:

    { "head": { "link": [], "vars": ["consolidatedCelex"] },
      "results": { "distinct": false, "ordered": true, "bindings": [
        { "consolidatedCelex": { "type": "literal",
          "datatype": "http://www.w3.org/2001/XMLSchema#string",
          "value": "02016R0679-20160504" }} ] } }

`fetch_consolidated_versions("32016R0679")` therefore yields
`ConsolidatedVersionInfo(base_celex="32016R0679",
consolidated_celexes=("02016R0679-20160504",))`, whose `latest_consolidation_date`
is `date(2016, 5, 4)`. The live assertion lives in
`tests/change_monitor/test_cellar_consolidated.py::test_ac001_live_consolidated_detection`.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Protocol, Self, TypeIs

from ps_service.change_monitor.errors import CellarConsolidationQueryError
from ps_service.dependency_health import CELLAR_ELI, mark_healthy, mark_unhealthy

_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
_TIMEOUT_SECONDS = 30.0
_HEADERS = {"Accept": "application/sparql-results+json"}
_RESULT_FORMAT = "application/sparql-results+json"
_HTTP_OK = 200

_BASE_CELEX_PATTERN = re.compile(r"^\d{5}[A-Z]{1,2}\d+$")
_LEGISLATION_SECTOR = "3"

_RESULT_VAR = "consolidatedCelex"

_QUERY_TEMPLATE = """\
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT ?consolidatedCelex WHERE {{
  ?base cdm:resource_legal_id_celex "{base_celex}"^^xsd:string .
  ?consolidatedWork cdm:act_consolidated_consolidates_resource_legal ?base ;
                    cdm:resource_legal_id_celex ?consolidatedCelex .
  FILTER(STRSTARTS(STR(?consolidatedCelex), "{prefix}"))
}}
ORDER BY DESC(?consolidatedCelex)"""


@dataclass(frozen=True, slots=True)
class ConsolidatedVersionInfo:
    """CELLAR's consolidated-expression state for one base act.

    `consolidated_celexes` is ascending (lexical == chronological, the suffix
    is zero-padded) and empty when CELLAR publishes no consolidated version.
    """

    base_celex: str
    consolidated_celexes: tuple[str, ...]

    @property
    def latest_celex(self) -> str | None:
        """The most recent consolidated CELEX, or `None` when there is none."""
        return self.consolidated_celexes[-1] if self.consolidated_celexes else None

    @property
    def latest_consolidation_date(self) -> date | None:
        """The `-YYYYMMDD` suffix of `latest_celex` as a `date`, or `None`."""
        latest = self.latest_celex
        if latest is None:
            return None
        return date(int(latest[11:15]), int(latest[15:17]), int(latest[17:19]))


class SparqlResponse(Protocol):
    """The minimal response shape `fetch_consolidated_versions` reads.

    A context manager exposing the HTTP status and a `read()` that yields the
    body bytes -- deliberately narrower than `typing.IO[bytes]` so a
    lightweight test double satisfies it structurally, matching what
    `urllib.request.urlopen` returns (`http.client.HTTPResponse`).
    """

    status: int
    """The HTTP status code of the response."""

    def read(self) -> bytes:
        """Return the full response body as bytes."""
        ...

    def __enter__(self) -> Self:
        """Enter the response context, returning the response itself."""
        ...

    def __exit__(self, *exc_info: object) -> None:
        """Exit the response context, releasing the underlying connection."""
        ...


class SparqlTransport(Protocol):
    """The DI seam `fetch_consolidated_versions` calls through.

    Matches `urllib.request.urlopen`'s call shape exactly (and
    `ps_service.ingestion.adapters.cellar_eli.fetch.CellarTransport`'s), so
    the real `urlopen` is the default with no wrapper, while a test can
    substitute a fake without monkeypatching `urllib`.
    """

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> SparqlResponse:
        """Perform the HTTP round-trip for `request`, returning the response."""
        ...


def fetch_consolidated_versions(
    base_celex: str,
    *,
    transport: SparqlTransport = urllib.request.urlopen,
) -> ConsolidatedVersionInfo:
    """Query CELLAR SPARQL for every consolidated expression of `base_celex`.

    `base_celex` is a base-act CELEX in the legislation sector (e.g.
    `32016R0679`); `transport` defaults to the real `urllib.request.urlopen`
    but is call-site injectable (L2 DI). The same call serves a `regulation`
    and a `directive` -- one code path, no per-instrument branching.

    Raises `CellarConsolidationQueryError` on a malformed / non-legislation
    `base_celex`, a transport failure, a non-200 response, or a body that is
    not the expected SPARQL-results JSON shape.
    """
    consolidated_prefix = _consolidated_celex_prefix(base_celex)
    query = _QUERY_TEMPLATE.format(base_celex=base_celex, prefix=consolidated_prefix)
    url = f"{_ENDPOINT}?{urllib.parse.urlencode({'query': query, 'format': _RESULT_FORMAT})}"
    if not url.startswith("https://"):
        raise CellarConsolidationQueryError(
            f"refusing non-https CELLAR SPARQL URL for base CELEX {base_celex!r}: {url}"
        )
    request = urllib.request.Request(url, headers=_HEADERS)  # noqa: S310 — https scheme enforced by the guard above
    body = _read_body(request, base_celex, transport)
    return ConsolidatedVersionInfo(
        base_celex=base_celex,
        consolidated_celexes=_parse_consolidated_celexes(body),
    )


def _read_body(
    request: urllib.request.Request,
    base_celex: str,
    transport: SparqlTransport,
) -> bytes:
    """Run `request` through `transport`, returning the body on a clean HTTP 200.

    Records the outcome in `ps_service.dependency_health` under the shared
    `CELLAR_ELI` key (same host as `cellar_eli.fetch`), mirroring that
    module. Raises `CellarConsolidationQueryError` on a transport failure or
    any non-200 status.
    """
    try:
        with transport(request, timeout=_TIMEOUT_SECONDS) as response:
            status = response.status
            body = response.read()
    except Exception as exc:
        mark_unhealthy(CELLAR_ELI, error=exc)
        raise CellarConsolidationQueryError(
            f"CELLAR SPARQL consolidated-version query failed for base CELEX "
            f"{base_celex!r} ({_ENDPOINT}): {exc}"
        ) from exc
    if status != _HTTP_OK:
        error = CellarConsolidationQueryError(
            f"CELLAR SPARQL consolidated-version query for base CELEX {base_celex!r} "
            f"returned HTTP {status} ({_ENDPOINT})"
        )
        mark_unhealthy(CELLAR_ELI, error=error)
        raise error
    mark_healthy(CELLAR_ELI)
    return body


def _consolidated_celex_prefix(base_celex: str) -> str:
    r"""The `0<base>-` FILTER prefix for a base-act CELEX in the legislation sector.

    Validates `base_celex` against `^\d{5}[A-Z]{1,2}\d+$` **and** asserts
    its sector digit is `3` (legislation) -- a consolidated-form CELEX
    (leading `0`, `-date` suffix) or a non-legislation sector is rejected
    with `CellarConsolidationQueryError` (PLAN_REVIEWED.md flaw 15). Returns
    `"0" + base_celex[1:] + "-"` -- never a blind character replace.
    """
    if _BASE_CELEX_PATTERN.match(base_celex) is None:
        raise CellarConsolidationQueryError(
            f"{base_celex!r} is not a base-act CELEX of the form "
            f"NNNNN<type-letters><number> (e.g. '32016R0679')"
        )
    if base_celex[0] != _LEGISLATION_SECTOR:
        raise CellarConsolidationQueryError(
            f"base CELEX {base_celex!r} is in sector {base_celex[0]!r}, not the "
            f"legislation sector {_LEGISLATION_SECTOR!r}; the change monitor "
            f"tracks legislation only"
        )
    return "0" + base_celex[1:] + "-"


def _parse_consolidated_celexes(body: bytes) -> tuple[str, ...]:
    """Extract the `consolidatedCelex` values from a SPARQL-results JSON body, ascending.

    Raises `CellarConsolidationQueryError` if `body` is not valid JSON or is
    not the expected `{"results": {"bindings": [...]}}` shape.
    """
    try:
        payload: object = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CellarConsolidationQueryError(
            f"CELLAR SPARQL response was not valid JSON: {exc}"
        ) from exc
    if not _is_json_object(payload):
        raise CellarConsolidationQueryError(
            f"CELLAR SPARQL response is not a JSON object: {payload!r}"
        )
    results = payload.get("results")
    if not _is_json_object(results):
        raise CellarConsolidationQueryError(
            f"CELLAR SPARQL response has no 'results' object: {payload!r}"
        )
    bindings = results.get("bindings")
    if not _is_json_array(bindings):
        raise CellarConsolidationQueryError(
            f"CELLAR SPARQL response 'results.bindings' is not a list: {results!r}"
        )
    return tuple(sorted(_binding_celex(binding) for binding in bindings))


def _binding_celex(binding: object) -> str:
    """The `consolidatedCelex` string of one SPARQL result binding.

    Raises `CellarConsolidationQueryError` if the binding is not the expected
    `{"consolidatedCelex": {"value": "<celex>"}}` shape.
    """
    if not _is_json_object(binding):
        raise CellarConsolidationQueryError(
            f"CELLAR SPARQL result binding is not an object: {binding!r}"
        )
    cell = binding.get(_RESULT_VAR)
    if not _is_json_object(cell):
        raise CellarConsolidationQueryError(
            f"CELLAR SPARQL result binding has no {_RESULT_VAR!r} object: {binding!r}"
        )
    value = cell.get("value")
    if not isinstance(value, str):
        raise CellarConsolidationQueryError(
            f"CELLAR SPARQL result binding {_RESULT_VAR!r} has no string 'value': {cell!r}"
        )
    return value


def _is_json_object(value: object) -> TypeIs[dict[str, object]]:
    """Narrow a `json.loads` result to a JSON object (keys are always strings)."""
    return isinstance(value, dict)


def _is_json_array(value: object) -> TypeIs[list[object]]:
    """Narrow a `json.loads` result to a JSON array."""
    return isinstance(value, list)
