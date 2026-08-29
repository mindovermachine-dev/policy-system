"""Tests for `ps_service.change_monitor.poll` (AC-002, AC-003, AC-009, AC-010).

Tests 5-11 (PLAN_REVIEWED.md §3): newer-consolidation detection, the
up-to-date case, `regulation` == `directive` path parity, per-instrument
CELLAR-failure isolation, `not_configured` vs `poll_failed`, the
conservative `baseline_unknown` fallback, and the one-log-entry-per-instrument
run-id contract.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from change_monitor._fakes import FakeGraph, FakeQueryResult, MakeEmitter, ReadLines
from ps_service.change_monitor.cellar_consolidated import ConsolidatedVersionInfo
from ps_service.change_monitor.errors import CellarConsolidationQueryError
from ps_service.change_monitor.poll import poll_for_amendments

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ps_service.change_monitor.falkordb_client import GraphHandle


def _info(base_celex: str, *consolidated: str) -> ConsolidatedVersionInfo:
    """A `ConsolidatedVersionInfo` for `base_celex` with the given consolidated CELEXes."""
    return ConsolidatedVersionInfo(
        base_celex=base_celex, consolidated_celexes=tuple(sorted(consolidated))
    )


class _FakeConsolidatedVersions:
    """Satisfies `ConsolidatedVersionLookup`: canned info per base CELEX.

    A base CELEX listed in `failing` raises `CellarConsolidationQueryError`
    instead, to drive the per-instrument `poll_failed` path.
    """

    def __init__(
        self,
        by_celex: Mapping[str, ConsolidatedVersionInfo],
        *,
        failing: frozenset[str] = frozenset(),
    ) -> None:
        self._by_celex = by_celex
        self._failing = failing
        self.calls: list[str] = []

    def __call__(self, base_celex: str) -> ConsolidatedVersionInfo:
        self.calls.append(base_celex)
        if base_celex in self._failing:
            raise CellarConsolidationQueryError(f"boom for {base_celex}")
        return self._by_celex[base_celex]


def _graph(*rows: list[object]) -> GraphHandle:
    """A `FakeGraph` primed with one enumeration result holding `rows`."""
    return FakeGraph([FakeQueryResult(list(rows))])


# --- Increment 6: tests 5, 6, 7 --------------------------------------------


def test_reports_instrument_with_newer_consolidation(make_emitter: MakeEmitter) -> None:
    emitter, _ = make_emitter()
    graph = FakeGraph(
        [
            FakeQueryResult(
                [
                    ["CRA-1.0", "32024R2847", "regulation", "2020-01-01"],
                    ["NIS2-1.0", "32022L2555", "directive", "2020-01-01"],
                ]
            )
        ]
    )
    lookup = _FakeConsolidatedVersions(
        {
            "32024R2847": _info("32024R2847", "02024R2847-20241120"),
            "32022L2555": _info("32022L2555", "02022L2555-20221227"),
        }
    )

    report = poll_for_amendments(graph, consolidated_versions=lookup, emitter=emitter)

    assert {f.regulatory_instrument_id for f in report.findings} == {"CRA-1.0", "NIS2-1.0"}
    cra = next(f for f in report.findings if f.regulatory_instrument_id == "CRA-1.0")
    assert cra.detected_consolidated_celex == "02024R2847-20241120"
    assert cra.detected_consolidation_date == date(2024, 11, 20)
    assert cra.reason == "newer_consolidation"
    assert cra.instrument_type == "regulation"
    assert len(graph.calls) == 1
    assert graph.writes == []


def test_reports_nothing_when_up_to_date(make_emitter: MakeEmitter, read_lines: ReadLines) -> None:
    emitter, log_path = make_emitter()
    graph = _graph(["CRA-1.0", "32024R2847", "regulation", "2025-06-01"])
    lookup = _FakeConsolidatedVersions({"32024R2847": _info("32024R2847", "02024R2847-20241120")})

    report = poll_for_amendments(graph, consolidated_versions=lookup, emitter=emitter)
    emitter.flush()

    assert report.findings == ()
    lines = read_lines(log_path)
    assert [line["outcome"] for line in lines] == ["current"]


@pytest.mark.parametrize("instrument_type", ["regulation", "directive"])
def test_directive_and_regulation_take_identical_path(
    instrument_type: str, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    graph = _graph(["X-1.0", "32024R2847", instrument_type, "2020-01-01"])
    lookup = _FakeConsolidatedVersions({"32024R2847": _info("32024R2847", "02024R2847-20241120")})

    report = poll_for_amendments(graph, consolidated_versions=lookup, emitter=emitter)
    emitter.flush()

    assert len(report.findings) == 1
    assert report.findings[0].instrument_type == instrument_type
    assert report.findings[0].reason == "newer_consolidation"
    (line,) = read_lines(log_path)
    assert line["action"] == "poll_for_amendments"
    assert line["outcome"] == "amendment_detected"


# --- Increment 7: tests 8, 9, 10, 11 -------------------------------------


def test_per_instrument_cellar_failure_is_isolated(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    graph = _graph(
        ["A-1.0", "32000R0001", "regulation", "2020-01-01"],
        ["B-1.0", "32000R0002", "regulation", "2020-01-01"],
        ["C-1.0", "32000R0003", "directive", "2020-01-01"],
    )
    lookup = _FakeConsolidatedVersions(
        {
            "32000R0001": _info("32000R0001", "02000R0001-20240101"),
            "32000R0003": _info("32000R0003", "02000R0003-20240101"),
        },
        failing=frozenset({"32000R0002"}),
    )

    report = poll_for_amendments(graph, consolidated_versions=lookup, emitter=emitter)
    emitter.flush()

    assert report.failed_ids == ("B-1.0",)
    assert {f.regulatory_instrument_id for f in report.findings} == {"A-1.0", "C-1.0"}
    outcomes = {line["entity_id"]: line["outcome"] for line in read_lines(log_path)}
    assert outcomes == {
        "A-1.0": "amendment_detected",
        "B-1.0": "poll_failed",
        "C-1.0": "amendment_detected",
    }


def test_missing_celex_is_not_configured_not_poll_failed(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    graph = _graph(
        ["LEGACY-1.0", None, "regulation", "2020-01-01"],
        ["CRA-1.0", "32024R2847", "regulation", "2020-01-01"],
    )
    lookup = _FakeConsolidatedVersions({"32024R2847": _info("32024R2847", "02024R2847-20241120")})

    report = poll_for_amendments(graph, consolidated_versions=lookup, emitter=emitter)
    emitter.flush()

    assert report.unconfigured_ids == ("LEGACY-1.0",)
    assert "LEGACY-1.0" not in report.failed_ids
    assert {f.regulatory_instrument_id for f in report.findings} == {"CRA-1.0"}
    outcomes = {line["entity_id"]: line["outcome"] for line in read_lines(log_path)}
    assert outcomes["LEGACY-1.0"] == "not_configured"
    assert lookup.calls == ["32024R2847"]


@pytest.mark.parametrize("effective_date", ["", "not-a-real-date"])
def test_baseline_unknown_is_conservative(
    effective_date: str, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    graph = _graph(["CRA-1.0", "32024R2847", "regulation", effective_date])
    lookup = _FakeConsolidatedVersions({"32024R2847": _info("32024R2847", "02024R2847-20241120")})

    report = poll_for_amendments(graph, consolidated_versions=lookup, emitter=emitter)
    emitter.flush()

    assert len(report.findings) == 1
    assert report.findings[0].reason == "baseline_unknown"
    assert report.findings[0].baseline_reference == "unknown"
    assert report.findings[0].detected_consolidated_celex == "02024R2847-20241120"
    (line,) = read_lines(log_path)
    assert line["outcome"] == "amendment_detected"


def test_override_baseline_suppresses_a_stale_effective_date(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    graph = _graph(["CRA-1.0", "32024R2847", "regulation", "2000-01-01"])
    lookup = _FakeConsolidatedVersions({"32024R2847": _info("32024R2847", "02024R2847-20241120")})

    report = poll_for_amendments(
        graph,
        baseline_overrides={"CRA-1.0": "02024R2847-20241120"},
        consolidated_versions=lookup,
        emitter=emitter,
    )

    assert report.findings == ()


def test_binds_own_run_id_one_entry_per_instrument(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    lookup = _FakeConsolidatedVersions(
        {"32000R0001": _info("32000R0001"), "32000R0002": _info("32000R0002")}
    )

    poll_for_amendments(
        _graph(
            ["A-1.0", "32000R0001", "regulation", "2020-01-01"],
            ["B-1.0", "32000R0002", "directive", "2020-01-01"],
        ),
        consolidated_versions=lookup,
        emitter=emitter,
    )
    poll_for_amendments(
        _graph(["A-1.0", "32000R0001", "regulation", "2020-01-01"]),
        consolidated_versions=lookup,
        emitter=emitter,
    )
    emitter.flush()

    lines = read_lines(log_path)
    assert len(lines) == 3
    assert all(line["component"] == "change_monitor" for line in lines)
    assert all(line["action"] == "poll_for_amendments" for line in lines)
    assert all(line["entity_id"] for line in lines)
    first_run = {line["run_id"] for line in lines[:2]}
    second_run = {line["run_id"] for line in lines[2:]}
    assert len(first_run) == 1
    assert len(second_run) == 1
    assert first_run != second_run
