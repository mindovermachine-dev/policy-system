"""Tests for `ps_service.change_monitor.cellar_consolidated` (AC-001).

0a -- unit, fake transport: multi-row parsing, derivations, error shapes,
      the flaw-15 base-CELEX guard.
0b -- `cellar_live`: the real CELLAR SPARQL endpoint.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date
from typing import TYPE_CHECKING, NoReturn, Self

import pytest

from ps_service.change_monitor.cellar_consolidated import (
    ConsolidatedVersionInfo,
    fetch_consolidated_versions,
)
from ps_service.change_monitor.errors import CellarConsolidationQueryError
from ps_service.dependency_health import CELLAR_ELI, is_healthy

if TYPE_CHECKING:
    from collections.abc import Iterable


def _results_body(*celexes: str) -> bytes:
    """A SPARQL-results JSON body binding `?consolidatedCelex` to each of `celexes`."""
    return json.dumps(
        {
            "head": {"vars": ["consolidatedCelex"]},
            "results": {
                "bindings": [
                    {
                        "consolidatedCelex": {
                            "type": "literal",
                            "datatype": "http://www.w3.org/2001/XMLSchema#string",
                            "value": celex,
                        }
                    }
                    for celex in celexes
                ]
            },
        }
    ).encode("utf-8")


class _FakeSparqlResponse:
    """Minimal stand-in for `urllib.request.urlopen`'s return: a context manager
    exposing `status` and a `read()` that yields canned body bytes.
    """

    def __init__(self, body: bytes, status: int) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _FakeSparqlTransport:
    """Satisfies `SparqlTransport`; records the requests it was called with so a
    test can assert the exact SPARQL URL/headers, and returns a canned body.
    """

    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self._body = body
        self._status = status
        self.requests: list[urllib.request.Request] = []

    def __call__(
        self, request: urllib.request.Request, /, *, timeout: float
    ) -> _FakeSparqlResponse:
        self.requests.append(request)
        return _FakeSparqlResponse(self._body, self._status)


class _FailingSparqlTransport:
    """A `SparqlTransport` whose round-trip always fails at the network level."""

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> NoReturn:
        raise TimeoutError("connection timed out")


# --- 0a: unit -----------------------------------------------------------------


def test_multi_row_body_parses_to_ascending_tuple() -> None:
    transport = _FakeSparqlTransport(
        _results_body("02013R0575-20260626", "02013R0575-20140101", "02013R0575-20130628")
    )

    info = fetch_consolidated_versions("32013R0575", transport=transport)

    assert info.consolidated_celexes == (
        "02013R0575-20130628",
        "02013R0575-20140101",
        "02013R0575-20260626",
    )


def test_latest_celex_is_the_lexical_max() -> None:
    transport = _FakeSparqlTransport(_results_body("02016R0679-20160504", "02016R0679-20180525"))

    info = fetch_consolidated_versions("32016R0679", transport=transport)

    assert info.latest_celex == "02016R0679-20180525"


def test_latest_consolidation_date_is_derived_from_the_suffix() -> None:
    transport = _FakeSparqlTransport(_results_body("02016R0679-20160504"))

    info = fetch_consolidated_versions("32016R0679", transport=transport)

    assert info.latest_consolidation_date == date(2016, 5, 4)


def test_empty_bindings_yield_no_latest() -> None:
    transport = _FakeSparqlTransport(_results_body())

    info = fetch_consolidated_versions("32024R2847", transport=transport)

    assert info.consolidated_celexes == ()
    assert info.latest_celex is None
    assert info.latest_consolidation_date is None


def test_the_request_carries_the_endpoint_query_and_accept_header() -> None:
    transport = _FakeSparqlTransport(_results_body())

    fetch_consolidated_versions("32016R0679", transport=transport)

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.full_url.startswith("https://publications.europa.eu/webapi/rdf/sparql?")
    assert request.get_header("Accept") == "application/sparql-results+json"
    decoded = urllib.parse.unquote_plus(request.full_url)
    assert 'cdm:resource_legal_id_celex "32016R0679"^^xsd:string' in decoded
    assert 'STRSTARTS(STR(?consolidatedCelex), "02016R0679-")' in decoded
    assert "format=application/sparql-results+json" in decoded


def test_non_200_response_raises_query_error_and_marks_unhealthy() -> None:
    transport = _FakeSparqlTransport(_results_body(), status=500)

    with pytest.raises(CellarConsolidationQueryError, match="HTTP 500"):
        fetch_consolidated_versions("32016R0679", transport=transport)

    assert is_healthy(CELLAR_ELI) is False


def test_transport_failure_raises_query_error_and_marks_unhealthy() -> None:
    with pytest.raises(CellarConsolidationQueryError) as exc_info:
        fetch_consolidated_versions("32016R0679", transport=_FailingSparqlTransport())

    assert isinstance(exc_info.value.__cause__, TimeoutError)
    assert "32016R0679" in str(exc_info.value)
    assert is_healthy(CELLAR_ELI) is False


def test_garbage_body_raises_query_error() -> None:
    transport = _FakeSparqlTransport(b"<html>not json</html>")

    with pytest.raises(CellarConsolidationQueryError, match="not valid JSON"):
        fetch_consolidated_versions("32016R0679", transport=transport)


def test_json_body_without_results_bindings_raises_query_error() -> None:
    transport = _FakeSparqlTransport(json.dumps({"head": {"vars": []}}).encode("utf-8"))

    with pytest.raises(CellarConsolidationQueryError):
        fetch_consolidated_versions("32016R0679", transport=transport)


def test_binding_without_a_string_value_raises_query_error() -> None:
    body = json.dumps(
        {"results": {"bindings": [{"consolidatedCelex": {"type": "literal"}}]}}
    ).encode("utf-8")
    transport = _FakeSparqlTransport(body)

    with pytest.raises(CellarConsolidationQueryError):
        fetch_consolidated_versions("32016R0679", transport=transport)


def test_successful_query_marks_cellar_healthy() -> None:
    transport = _FakeSparqlTransport(_results_body("02016R0679-20160504"))

    fetch_consolidated_versions("32016R0679", transport=transport)

    assert is_healthy(CELLAR_ELI) is True


@pytest.mark.parametrize(
    "bad_celex",
    [
        "02016R0679-20160504",  # already a consolidated-form CELEX
        "32016R0679-20160504",  # base sector but a trailing -date suffix
        "GDPR",
        "32016",
        "3201R0679",  # only 4 leading digits
    ],
)
def test_malformed_base_celex_is_rejected_before_any_network_call(bad_celex: str) -> None:
    transport = _FakeSparqlTransport(_results_body())

    with pytest.raises(CellarConsolidationQueryError):
        fetch_consolidated_versions(bad_celex, transport=transport)

    assert transport.requests == []


def test_non_legislation_sector_celex_is_rejected_naming_the_sector_digit() -> None:
    transport = _FakeSparqlTransport(_results_body())

    with pytest.raises(CellarConsolidationQueryError, match=r"sector '1'"):
        fetch_consolidated_versions("12016R0679", transport=transport)

    assert transport.requests == []


@pytest.mark.parametrize(
    ("celexes", "expected_latest_date"),
    [
        (("02016R0679-20160504",), date(2016, 5, 4)),
        ((), None),
    ],
)
def test_consolidated_version_info_derivations(
    celexes: Iterable[str], expected_latest_date: date | None
) -> None:
    info = ConsolidatedVersionInfo(base_celex="32016R0679", consolidated_celexes=tuple(celexes))

    assert info.latest_consolidation_date == expected_latest_date


# --- 0b: cellar_live --------------------------------------------------------


@pytest.mark.cellar_live
def test_ac001_live_consolidated_detection() -> None:
    """Real CELLAR SPARQL endpoint -- no local fixture, per AC-001.

    GDPR (`32016R0679`) has exactly one consolidated expression
    (`02016R0679-20160504`); CRR (`32013R0575`) has a growing series whose
    latest is well past `02013R0575-20140101`.
    """
    gdpr = fetch_consolidated_versions("32016R0679")

    assert gdpr.consolidated_celexes == ("02016R0679-20160504",)
    assert gdpr.latest_celex == "02016R0679-20160504"
    assert gdpr.latest_consolidation_date == date(2016, 5, 4)

    crr = fetch_consolidated_versions("32013R0575")

    assert crr.latest_celex is not None
    assert crr.latest_celex.startswith("02013R0575-")
    assert crr.latest_celex > "02013R0575-20140101"
