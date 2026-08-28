"""Live end-to-end test for `ps_service.ingestion.pipeline.ingest_regulation`
(PLAN_REVIEWED.md §7 Increment 13) — the capstone acceptance proof for all
7 of issue #14's acceptance criteria, run for real against the live
Cellar/ELI service (`publications.europa.eu`) and a real, reachable
FalkorDB instance.

`@pytest.mark.cellar_live @pytest.mark.falkordb_live` on every test in this
module (via `pytestmark`): these tests make real network calls and write
real data to FalkorDB — they are excluded from the fast regression suite
(`-m "not cellar_live and not falkordb_live"`) and must be run explicitly.

Connection is built via `connect_from_config(load_config())` (Increment 12,
S4 fix) — never hardcoded `"127.0.0.1"`/`6379` literals — proving the
config surface Increment 7 added is genuinely load-bearing.

`live_runs`, below, is a module-scoped fixture: it pays the real
network/DB cost of ingesting all three regulations exactly once; every
`test_ac_0*` function then asserts one slice of that same real run's
results against `spikes/cellar1/LEARNINGS.md`'s independently-confirmed
ground truth. This keeps each AC's evidence separately readable (and
separately reportable in pytest's output) without re-running the live
ingestion three times over.

AC-006 note (regulation-independence): already proven at the code level by
`test_pipeline.py`'s AST scan (Increment 11) — this file does not
re-litigate that proof. This file's own module-level `_GROUND_TRUTH` dict,
keyed by regulation short name, is test code identifying which regulation
to assert against — expected and explicitly not what AC-006 restricts
(CONTEXT.md's task brief for this batch says so directly); AC-006's scan
only walks `ps_service/ingestion/` source, never `tests/`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

import pytest

from ps_service.config import load_config
from ps_service.ingestion.adapters.cellar_eli import CellarEliAdapter
from ps_service.ingestion.falkordb_client import (
    GraphHandle,
    check_connectivity,
    connect_from_config,
    native_graph_name,
    select_graph,
)
from ps_service.ingestion.models import IngestResult
from ps_service.ingestion.pipeline import ingest_regulation
from ps_service.logging import EmitterConfig, LogEmitter

pytestmark = [pytest.mark.cellar_live, pytest.mark.falkordb_live]

_VERSION = "1.0"

# Ground truth cross-checked against spikes/cellar1/LEARNINGS.md's
# independently-confirmed table (fetched live, verified against public
# knowledge of each regulation's real structure — see LEARNINGS.md's own
# cross-check notes for CRA's PDF-based extraction and GDPR's known-zero
# annex count).
_GROUND_TRUTH: dict[str, dict[str, object]] = {
    "CRA": {
        "identifier": "32024R2847",
        "counts": {"ARTICLE": 71, "ANNEX": 8, "RECITAL": 130},
        "effective_date": date(2027, 12, 11),
        "instrument_type": "regulation",
    },
    "NIS2": {
        "identifier": "32022L2555",
        "counts": {"ARTICLE": 46, "ANNEX": 3, "RECITAL": 144},
        "effective_date": date(2024, 10, 17),
        "instrument_type": "directive",
    },
    "GDPR": {
        "identifier": "32016R0679",
        "counts": {"ARTICLE": 99, "ANNEX": 0, "RECITAL": 173},
        "effective_date": date(2018, 5, 25),
        "instrument_type": "regulation",
    },
}


@dataclass(frozen=True)
class _LiveRun:
    """One regulation's real ingestion outcome, plus the graph handle used
    to query FalkorDB for the fields `IngestResult` doesn't itself carry
    (title/jurisdiction/effective_date/... live on the persisted node, not
    on the result object)."""

    short_name: str
    graph: GraphHandle
    ingest_result: IngestResult
    log_path: Path


@pytest.fixture(scope="module")
def live_runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, _LiveRun]:
    """Ingest CRA, NIS2, and GDPR for real, once, sharing the cost across
    every `test_ac_0*` function below.

    Cleans up any pre-existing `cra_native`/`gdpr_native`/`nis2_native`
    graphs first (from a previous partial run), so this is a clean,
    reproducible run — local dev FalkorDB data, safe to reset.
    """
    config = load_config()
    db = connect_from_config(config)
    check_connectivity(db, host=config.falkordb_host, port=config.falkordb_port)

    existing_graph_names = set(db.list_graphs())
    for short_name in _GROUND_TRUTH:
        graph_name = native_graph_name(short_name)
        if graph_name in existing_graph_names:
            db.select_graph(graph_name).delete()

    log_path = tmp_path_factory.mktemp("ingestion_live") / "ingestion_live.jsonl"
    emitter = LogEmitter(EmitterConfig(log_path=log_path))
    # Real, default `fetch_xhtml` transport — genuinely live, no mocking.
    adapter = CellarEliAdapter()

    runs: dict[str, _LiveRun] = {}
    for short_name, spec in _GROUND_TRUTH.items():
        graph = select_graph(db, native_graph_name(short_name))
        # AC-001: this call's own path is
        #   ingest_regulation -> adapter.fetch_regulation_structure
        #   (CellarEliAdapter) -> fetch_xhtml (real HTTP GET to
        #   publications.europa.eu/resource/celex/{identifier}) ->
        #   extract_metadata/parse_structure (parse the fetched bytes,
        #   in memory).
        # No local file, fixture, or PDF is read anywhere in this call
        # graph — self-evident from the imports above (no `open()`, no
        # `Path.read_*`, no fixture directory reference anywhere in this
        # fixture or in pipeline.py/adapter.py's own source).
        result = ingest_regulation(
            cast(str, spec["identifier"]),
            short_name,
            version=_VERSION,
            adapter=adapter,
            graph=graph,
            emitter=emitter,
        )
        runs[short_name] = _LiveRun(
            short_name=short_name, graph=graph, ingest_result=result, log_path=log_path
        )
    emitter.flush()
    return runs


def _regulation_row(run: _LiveRun) -> list[object]:
    """Fetch the persisted Regulation node's bibliographic fields back
    from FalkorDB, by the exact id `ingest_regulation()` reported."""
    result = run.graph.query(
        "MATCH (n:RegulatoryInstrument {id: $id}) "
        "RETURN n.title, n.jurisdiction, n.effective_date, n.version, n.status, n.source_type, "
        "n.instrument_type",
        params={"id": run.ingest_result.regulation_id},
    )
    rows = cast("list[list[object]]", result.result_set)
    assert len(rows) == 1, f"expected exactly 1 Regulation node for {run.ingest_result.regulation_id!r}, got {len(rows)}"
    return rows[0]


# --- AC-001: live fetch, all 3, no local file/fixture/PDF ------------------


def test_ac001_ingestion_succeeds_live_for_all_three_regulations(
    live_runs: dict[str, _LiveRun],
) -> None:
    """All three `ingest_regulation()` calls in the `live_runs` fixture
    completed without raising `CellarFetchError`/`CellarParseError` — i.e.
    the live Cellar/ELI fetch genuinely succeeded for CRA, NIS2, and GDPR.
    See the fixture's own comment for the no-local-source call-graph proof.
    """
    assert set(live_runs) == set(_GROUND_TRUTH)
    for short_name, run in live_runs.items():
        assert run.ingest_result.regulation_id == f"{short_name}-{_VERSION}"


# --- AC-002: Regulation node id + 6 bibliographic fields --------------------


@pytest.mark.parametrize("short_name", sorted(_GROUND_TRUTH))
def test_ac002_regulation_node_id_and_bibliographic_fields(
    live_runs: dict[str, _LiveRun], short_name: str
) -> None:
    run = live_runs[short_name]
    expected_id = f"{short_name}-{_VERSION}"
    assert run.ingest_result.regulation_id == expected_id

    title, jurisdiction, effective_date_raw, version, status, source_type, instrument_type = (
        _regulation_row(run)
    )

    assert isinstance(title, str) and title.strip()
    assert isinstance(jurisdiction, str) and jurisdiction.strip()
    assert isinstance(version, str) and version.strip()
    assert isinstance(status, str) and status.strip()
    assert isinstance(source_type, str) and source_type.strip()

    assert jurisdiction == "EU"
    assert version == _VERSION
    assert status == "active"
    assert source_type == "external"

    # effective_date is persisted as an ISO-8601 string (FalkorDB query
    # params don't accept a raw `datetime.date`) — round-trip it back into
    # a real `date` object here to prove it survived the write/read cycle
    # as a genuine date value, not opaque/malformed text.
    assert isinstance(effective_date_raw, str)
    retrieved_date = date.fromisoformat(effective_date_raw)
    assert retrieved_date == _GROUND_TRUTH[short_name]["effective_date"]

    assert instrument_type == _GROUND_TRUTH[short_name]["instrument_type"]


# --- AC-003: every recital/article/annex persisted --------------------------


@pytest.mark.parametrize("short_name", sorted(_GROUND_TRUTH))
def test_ac003_structural_element_counts_match_learnings_ground_truth(
    live_runs: dict[str, _LiveRun], short_name: str
) -> None:
    run = live_runs[short_name]
    expected_counts = cast("dict[str, int]", _GROUND_TRUTH[short_name]["counts"])

    # element_type values here are this module's own fixed dict keys
    # (ARTICLE/ANNEX/RECITAL), never externally sourced — safe to
    # interpolate, matching graph_writer.py's own allow-listed-constant
    # convention.
    for element_type, expected_count in expected_counts.items():
        result = run.graph.query(f"MATCH (n:{element_type}) RETURN count(n)")
        rows = cast("list[list[object]]", result.result_set)
        actual_count = cast(int, rows[0][0])
        assert actual_count == expected_count, (
            f"{short_name} {element_type}: expected {expected_count} (LEARNINGS.md), got {actual_count}"
        )


# --- AC-004: every Article/Annex/... reachable from Regulation --------------


@pytest.mark.parametrize("short_name", sorted(_GROUND_TRUTH))
def test_ac004_every_structural_label_fully_reachable_from_regulation_node(
    live_runs: dict[str, _LiveRun], short_name: str
) -> None:
    """`ingest_regulation()`'s own `verify_structural_graph_reachable` call
    already raised `IngestionPersistenceError` during the `live_runs`
    fixture if any label had a reachability gap — the fixture completing at
    all is already a strong proof. This test additionally re-asserts the
    returned `ReachabilityCount`s explicitly, and cross-checks the
    structural labels' totals against LEARNINGS.md, so the evidence is
    visible per-AC rather than only implied by "the fixture didn't raise".
    """
    counts = live_runs[short_name].ingest_result.counts
    assert counts, "verify_structural_graph_reachable returned no labels"
    for label, reachability in counts.items():
        assert reachability.reachable == reachability.total, (
            f"{short_name} {label}: reachable={reachability.reachable} != total={reachability.total}"
        )

    expected_counts = cast("dict[str, int]", _GROUND_TRUTH[short_name]["counts"])
    for element_type, expected_count in expected_counts.items():
        assert counts[element_type].total == expected_count


# --- AC-005: 3 distinct run_ids, correlated in structured logs -------------


def test_ac005_three_distinct_run_ids_captured_across_the_three_runs(
    live_runs: dict[str, _LiveRun],
) -> None:
    run_ids_by_short_name = {
        short_name: run.ingest_result.run_id for short_name, run in live_runs.items()
    }
    assert len(set(run_ids_by_short_name.values())) == 3, (
        f"expected 3 distinct run_ids, got {run_ids_by_short_name}"
    )

    log_path = next(iter(live_runs.values())).log_path
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert lines, "no log entries were written — logging wiring bug"

    for short_name, run in live_runs.items():
        entity_id = run.ingest_result.regulation_id
        entity_run_ids = {line["run_id"] for line in lines if line.get("entity_id") == entity_id}
        assert entity_run_ids == {run.ingest_result.run_id}, (
            f"{short_name}: expected only run_id {run.ingest_result.run_id!r} on its own log "
            f"entries, got {entity_run_ids}"
        )


# --- AC-007: NIS2 transposition deadline vs. CRA/GDPR application dates ----


def test_ac007_nis2_effective_date_is_the_transposition_deadline(
    live_runs: dict[str, _LiveRun],
) -> None:
    """The Directive-specific case: NIS2's `effective_date` must be the
    Member-State transposition deadline (Art. 41), not the Directive's own
    EU-level entry-into-force date — the CA doc's Regulation mapping row
    convention (PLAN_REVIEWED.md §0.1/§3.2)."""
    _, _, effective_date_raw, _, _, _, _ = _regulation_row(live_runs["NIS2"])
    assert isinstance(effective_date_raw, str)
    assert date.fromisoformat(effective_date_raw) == date(2024, 10, 17)


@pytest.mark.parametrize(
    ("short_name", "expected_date"),
    [
        ("CRA", date(2027, 12, 11)),
        ("GDPR", date(2018, 5, 25)),
    ],
)
def test_ac007_regulation_effective_dates_are_the_application_date(
    live_runs: dict[str, _LiveRun], short_name: str, expected_date: date
) -> None:
    """The Regulation case (not a Directive): CRA/GDPR's `effective_date` is
    the "shall apply from" application date, via the same unconditional,
    text-driven search NIS2 uses — confirming the Entry-into-force fallback
    path generalizes across two independent Regulations, not just one."""
    _, _, effective_date_raw, _, _, _, _ = _regulation_row(live_runs[short_name])
    assert isinstance(effective_date_raw, str)
    assert date.fromisoformat(effective_date_raw) == expected_date
