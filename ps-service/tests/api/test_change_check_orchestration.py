"""Unit tests for `run_change_check_sweep` (issue #73, PLAN.md §4 Slices 2-3).

Exercises the real `ChangeCheckDependencies`-driven sweep against
`build_fake_change_check_dependencies` doubles: no real graph, poll, or
re-ingest call is ever made. Slice 2 covers the poll-stage buckets
(`current` / `poll_failed` / `not_configured`, AC-BI-003/AC-BI-005
partial/AC-BI-006 poll half). Slice 3 wires the `finding_ids` branch for
real -- `_reingest_one`'s D2-D7 call contract (AC-BI-004, AC-BI-005 core).
Slice 6 adds the dedicated `_emit_sweep`/`_emit_instrument` run-id
proof (AC-BI-008 full, D8) -- a real `LogEmitter`, not a stub, mirroring
`test_ingestion_orchestration.py`'s own
`test_emits_started_and_succeeded_entries_with_run_id_source_and_caller_and_duration`
shape. Slice 7 adds the dedicated AC-BI-009 proof: exactly-once
processing, no fan-out, no reprocessing within one sweep run -- a direct,
executed proof over Slice 2-5's already-shipped single-pass loop (PLAN.md
§1 D2), not just an inspection claim.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from api._fakes import (
    FakeGraphHandle,
    FakeIngestionAdapter,
    build_fake_change_check_dependencies,
)
from ps_service.api.catalog import CatalogEntry
from ps_service.api.change_check_orchestration import run_change_check_sweep
from ps_service.change_monitor.models import (
    AmendmentFinding,
    PollReport,
    ReingestionOutcome,
    TrackedInstrumentNode,
)

if TYPE_CHECKING:
    from api._fakes import MakeEmitter, ReadLines
    from ps_service.config import ServiceConfig


def _node(instrument_id: str) -> TrackedInstrumentNode:
    return TrackedInstrumentNode(
        regulatory_instrument_id=instrument_id,
        celex="32024R2847",
        instrument_type="regulation",
        effective_date="2024-01-01",
    )


def test_sweep_reports_current_for_every_tracked_instrument_when_poll_finds_nothing(
    app_config: ServiceConfig,
    make_emitter: MakeEmitter,
) -> None:
    """3 tracked nodes, an empty `PollReport` -> every outcome is `current`."""
    emitter, _ = make_emitter()
    tracked = (_node("A"), _node("B"), _node("C"))
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(findings=(), polled_count=3, failed_ids=(), unconfigured_ids=()),
    )

    result = run_change_check_sweep(
        config=app_config, run_id="r1", dependencies=fake.dependencies, emitter=emitter
    )

    actual = [(o.instrument_id, o.outcome, o.detail, o.reingest_run_id) for o in result.instruments]
    assert actual == [
        ("A", "current", None, None),
        ("B", "current", None, None),
        ("C", "current", None, None),
    ]


def test_sweep_reports_poll_failed_and_not_configured_from_the_poll_reports_own_buckets(
    app_config: ServiceConfig,
    make_emitter: MakeEmitter,
) -> None:
    """3 tracked nodes, `PollReport(failed_ids=(id_a,), unconfigured_ids=(id_b,))` ->
    `id_a` -> `poll_failed`, `id_b` -> `not_configured`, the third -> `current`.
    """
    emitter, _ = make_emitter()
    tracked = (_node("id_a"), _node("id_b"), _node("id_c"))
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(
            findings=(), polled_count=3, failed_ids=("id_a",), unconfigured_ids=("id_b",)
        ),
    )

    result = run_change_check_sweep(
        config=app_config, run_id="r1", dependencies=fake.dependencies, emitter=emitter
    )

    outcomes = {o.instrument_id: o.outcome for o in result.instruments}
    assert outcomes == {"id_a": "poll_failed", "id_b": "not_configured", "id_c": "current"}


def test_sweep_opens_the_single_tenant_graph_and_passes_it_to_poll_for_amendments(
    app_config: ServiceConfig,
    make_emitter: MakeEmitter,
) -> None:
    """The fake `poll_for_amendments` callable is invoked with the exact
    `FakeGraphHandle` instance `open_single_tenant` returned (AC-BI-003's
    "against the merged `policy_system` graph").
    """
    emitter, _ = make_emitter()
    fake = build_fake_change_check_dependencies(tracked=(_node("A"),))

    run_change_check_sweep(
        config=app_config, run_id="r1", dependencies=fake.dependencies, emitter=emitter
    )

    assert fake.poll_for_amendments_graphs == [fake.single_tenant]


def test_sweep_calls_read_tracked_instruments_exactly_once(
    app_config: ServiceConfig, make_emitter: MakeEmitter
) -> None:
    """D2's own "once" claim, made concrete: `read_tracked_instruments`'s call count is 1."""
    emitter, _ = make_emitter()
    fake = build_fake_change_check_dependencies(tracked=(_node("A"), _node("B")))

    run_change_check_sweep(
        config=app_config, run_id="r1", dependencies=fake.dependencies, emitter=emitter
    )

    assert len(fake.read_tracked_instruments_graphs) == 1


def _amendment_finding(instrument_id: str = "CRA-1.0") -> AmendmentFinding:
    return AmendmentFinding(
        regulatory_instrument_id=instrument_id,
        instrument_type="regulation",
        baseline_reference="2024-01-01",
        detected_consolidated_celex="32024R2847C01",
        detected_consolidation_date=date(2025, 1, 1),
        reason="newer_consolidation",
    )


def test_sweep_calls_trigger_reingestion_with_the_exact_ac_bi_004_argument_contract(
    app_config: ServiceConfig,
    make_emitter: MakeEmitter,
) -> None:
    """`trigger_reingestion` is called with the exact D4 argument contract: positional
    `identifier`/`short_name`/`new_version`, keyword `adapter`/`graph` bound to the
    exact objects `default_adapter`/`open_native` returned (AC-BI-004's direct proof).
    """
    emitter, _ = make_emitter()
    tracked = (_node("CRA-1.0"),)
    entry = CatalogEntry(
        celex="32024R2847", title="Cyber Resilience Act", short_name="CRA", version="1.0"
    )
    reingestion_result = ReingestionOutcome(
        prior_regulatory_instrument_id="CRA-0.9",
        new_regulatory_instrument_id="CRA-1.0",
        run_id="ingest-run-1",
        outcome="superseded",
        ingest_counts={},
    )
    sentinel_graph = FakeGraphHandle()
    sentinel_adapter = FakeIngestionAdapter()
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(
            findings=(_amendment_finding("CRA-1.0"),),
            polled_count=1,
            failed_ids=(),
            unconfigured_ids=(),
        ),
        catalog_entries={"32024R2847": entry},
        reingestion_result=reingestion_result,
        native_graph=sentinel_graph,
        ingestion_adapter=sentinel_adapter,
    )

    run_change_check_sweep(
        config=app_config, run_id="r1", dependencies=fake.dependencies, emitter=emitter
    )

    assert len(fake.trigger_reingestion_calls) == 1
    call = fake.trigger_reingestion_calls[0]
    assert call.identifier == "32024R2847"
    assert call.short_name == "CRA"
    assert call.new_version == "32024R2847C01"
    assert call.adapter is sentinel_adapter
    assert call.graph is sentinel_graph
    assert fake.open_native_short_names == ["CRA"]


def test_sweep_reports_amendment_reingested_with_detail_and_reingest_run_id_on_fresh_supersession(
    app_config: ServiceConfig,
    make_emitter: MakeEmitter,
) -> None:
    """A fresh (`superseded`) `trigger_reingestion` result -> `amendment_reingested`,
    `reingest_run_id` carrying the real re-ingest run id, `detail` naming both the new
    instrument id and `superseded` (D7).
    """
    emitter, _ = make_emitter()
    tracked = (_node("CRA-1.0"),)
    entry = CatalogEntry(
        celex="32024R2847", title="Cyber Resilience Act", short_name="CRA", version="1.0"
    )
    reingestion_result = ReingestionOutcome(
        prior_regulatory_instrument_id="CRA-0.9",
        new_regulatory_instrument_id="CRA-1.0",
        run_id="ingest-run-1",
        outcome="superseded",
        ingest_counts={},
    )
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(
            findings=(_amendment_finding("CRA-1.0"),),
            polled_count=1,
            failed_ids=(),
            unconfigured_ids=(),
        ),
        catalog_entries={"32024R2847": entry},
        reingestion_result=reingestion_result,
    )

    result = run_change_check_sweep(
        config=app_config, run_id="r1", dependencies=fake.dependencies, emitter=emitter
    )

    (outcome,) = result.instruments
    assert outcome.outcome == "amendment_reingested"
    assert outcome.reingest_run_id == "ingest-run-1"
    assert outcome.detail is not None
    assert "CRA-1.0" in outcome.detail
    assert "superseded" in outcome.detail


def test_sweep_reports_amendment_reingested_with_none_run_id_on_already_processed(
    app_config: ServiceConfig,
    make_emitter: MakeEmitter,
) -> None:
    """An `already_processed` `trigger_reingestion` result -> still `amendment_reingested`,
    but `reingest_run_id is None` and `detail` names `already_processed` (D7, the
    cross-run staleness case, PLAN.md §0.3).
    """
    emitter, _ = make_emitter()
    tracked = (_node("CRA-1.0"),)
    entry = CatalogEntry(
        celex="32024R2847", title="Cyber Resilience Act", short_name="CRA", version="1.0"
    )
    reingestion_result = ReingestionOutcome(
        prior_regulatory_instrument_id="CRA-0.9",
        new_regulatory_instrument_id="CRA-1.0",
        run_id=None,
        outcome="already_processed",
        ingest_counts=None,
    )
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(
            findings=(_amendment_finding("CRA-1.0"),),
            polled_count=1,
            failed_ids=(),
            unconfigured_ids=(),
        ),
        catalog_entries={"32024R2847": entry},
        reingestion_result=reingestion_result,
    )

    result = run_change_check_sweep(
        config=app_config, run_id="r1", dependencies=fake.dependencies, emitter=emitter
    )

    (outcome,) = result.instruments
    assert outcome.outcome == "amendment_reingested"
    assert outcome.reingest_run_id is None
    assert outcome.detail is not None
    assert "already_processed" in outcome.detail


def test_sweep_reports_reingest_failed_when_no_catalog_entry_resolves_the_finding_celex(
    app_config: ServiceConfig,
    make_emitter: MakeEmitter,
) -> None:
    """No curated catalog entry for the finding's base-act CELEX -> `reingest_failed`,
    `detail` names the celex, and `trigger_reingestion`/`open_native`/`default_adapter`
    are never called (D5 -- the missing-entry short-circuit happens before any write;
    the fake's `_never_*` stand-ins raise `AssertionError` if any of them is reached).
    """
    emitter, _ = make_emitter()
    tracked = (_node("CRA-1.0"),)
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(
            findings=(_amendment_finding("CRA-1.0"),),
            polled_count=1,
            failed_ids=(),
            unconfigured_ids=(),
        ),
        catalog_entries={},
    )

    result = run_change_check_sweep(
        config=app_config, run_id="r1", dependencies=fake.dependencies, emitter=emitter
    )

    (outcome,) = result.instruments
    assert outcome.outcome == "reingest_failed"
    assert outcome.detail is not None
    assert "32024R2847" in outcome.detail
    assert outcome.reingest_run_id is None
    assert fake.find_catalog_entry_calls == ["32024R2847"]


class NationalTranspositionNotSupportedError(Exception):
    """Locally-defined test double proving D10's name-matching works without
    importing the real `ps_service.change_monitor.errors` type (PLAN.md §4
    Slice 4).
    """


def test_sweep_reports_skipped_when_trigger_reingestion_raises_national_transposition_not_supported(
    app_config: ServiceConfig,
    make_emitter: MakeEmitter,
) -> None:
    """`trigger_reingestion` raising an exception literally named
    `NationalTranspositionNotSupportedError` -> `skipped`, `detail` is
    `str(exc)` verbatim (D10), and the sweep's next tracked instrument
    (scripted after this one, no finding of its own) still reports its own
    correct outcome (continuation proof, folds in AC-BI-006).
    """
    emitter, _ = make_emitter()
    tracked = (_node("CRA-1.0"), _node("GDPR-1.0"))
    entry = CatalogEntry(
        celex="32024R2847", title="Cyber Resilience Act", short_name="CRA", version="1.0"
    )
    exc = NationalTranspositionNotSupportedError(
        "Re-ingestion of a national_transposition instrument is not supported..."
    )
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(
            findings=(_amendment_finding("CRA-1.0"),),
            polled_count=2,
            failed_ids=(),
            unconfigured_ids=(),
        ),
        catalog_entries={"32024R2847": entry},
        reingestion_results=(exc,),
    )

    result = run_change_check_sweep(
        config=app_config, run_id="r1", dependencies=fake.dependencies, emitter=emitter
    )

    outcomes = {o.instrument_id: o for o in result.instruments}
    assert outcomes["CRA-1.0"].outcome == "skipped"
    assert outcomes["CRA-1.0"].detail == str(exc)
    assert outcomes["GDPR-1.0"].outcome == "current"


def test_sweep_does_not_confuse_a_different_exception_type_sharing_no_special_name_with_skipped(
    app_config: ServiceConfig,
    make_emitter: MakeEmitter,
) -> None:
    """A plain `RuntimeError` (no special name) -> `reingest_failed`, not
    `skipped` -- disproves any substring/prefix match on D10's name check.
    """
    emitter, _ = make_emitter()
    tracked = (_node("CRA-1.0"),)
    entry = CatalogEntry(
        celex="32024R2847", title="Cyber Resilience Act", short_name="CRA", version="1.0"
    )
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(
            findings=(_amendment_finding("CRA-1.0"),),
            polled_count=1,
            failed_ids=(),
            unconfigured_ids=(),
        ),
        catalog_entries={"32024R2847": entry},
        reingestion_results=(RuntimeError("boom"),),
    )

    result = run_change_check_sweep(
        config=app_config, run_id="r1", dependencies=fake.dependencies, emitter=emitter
    )

    (outcome,) = result.instruments
    assert outcome.outcome == "reingest_failed"


class ChangeMonitorStateError(Exception):
    """Name-agnostic test double for a generic reingest-stage failure (PLAN.md
    §4 Slice 5, D6) -- this bucket catches *anything* not name-matched to
    the national-transposition case; the exact class name is irrelevant to
    D10's check, only chosen to read as a plausible real failure.
    """


def test_sweep_isolates_a_reingest_failure_and_continues_to_remaining_instruments(
    app_config: ServiceConfig,
    make_emitter: MakeEmitter,
) -> None:
    """Two tracked nodes, both with findings; the first's `trigger_reingestion`
    raises a plain, name-agnostic exception double, the second succeeds
    normally -> the first outcome is `reingest_failed` (scrubbed `detail`),
    the second is `amendment_reingested` -- both present in the final
    result (AC-BI-006's isolation + continuation proof, at the reingest
    stage specifically, complementing Slice 2's poll-stage proof).
    """
    emitter, _ = make_emitter()
    tracked = (_node("CRA-1.0"), _node("GDPR-1.0"))
    entry = CatalogEntry(
        celex="32024R2847", title="Cyber Resilience Act", short_name="CRA", version="1.0"
    )
    reingestion_success = ReingestionOutcome(
        prior_regulatory_instrument_id="GDPR-0.9",
        new_regulatory_instrument_id="GDPR-1.0",
        run_id="ingest-run-2",
        outcome="superseded",
        ingest_counts={},
    )
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(
            findings=(_amendment_finding("CRA-1.0"), _amendment_finding("GDPR-1.0")),
            polled_count=2,
            failed_ids=(),
            unconfigured_ids=(),
        ),
        catalog_entries={"32024R2847": entry},
        reingestion_results=(ChangeMonitorStateError("boom"), reingestion_success),
    )

    result = run_change_check_sweep(
        config=app_config, run_id="r1", dependencies=fake.dependencies, emitter=emitter
    )

    outcomes = {o.instrument_id: o for o in result.instruments}
    assert outcomes["CRA-1.0"].outcome == "reingest_failed"
    assert outcomes["CRA-1.0"].detail is not None
    assert outcomes["GDPR-1.0"].outcome == "amendment_reingested"
    assert outcomes["GDPR-1.0"].reingest_run_id == "ingest-run-2"


def test_reingest_failed_detail_is_scrubbed_and_length_capped(
    app_config: ServiceConfig, make_emitter: MakeEmitter
) -> None:
    """A `trigger_reingestion` exception whose message embeds an absolute
    filesystem path and a `host:port` token -> the returned `detail`
    contains neither raw substring (`_scrub_text`'s own already-tested
    placeholders instead), and `len(detail) <= 300` (D11).
    """
    emitter, _ = make_emitter()
    tracked = (_node("CRA-1.0"),)
    entry = CatalogEntry(
        celex="32024R2847", title="Cyber Resilience Act", short_name="CRA", version="1.0"
    )
    leaky_path = "/very/deep/absolute/filesystem/path/to/some/internal/module.py"
    leaky_addr = "internal-host.example.com:6379"
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(
            findings=(_amendment_finding("CRA-1.0"),),
            polled_count=1,
            failed_ids=(),
            unconfigured_ids=(),
        ),
        catalog_entries={"32024R2847": entry},
        reingestion_results=(
            RuntimeError(f"connection to {leaky_addr} failed while reading {leaky_path}"),
        ),
    )

    result = run_change_check_sweep(
        config=app_config, run_id="r1", dependencies=fake.dependencies, emitter=emitter
    )

    (outcome,) = result.instruments
    assert outcome.outcome == "reingest_failed"
    assert outcome.detail is not None
    assert leaky_path not in outcome.detail
    assert leaky_addr not in outcome.detail
    assert len(outcome.detail) <= 300


def test_sweep_emits_started_and_succeeded_change_check_sweep_entries_carrying_the_run_id(
    app_config: ServiceConfig,
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    """AC-BI-008 (full, D8): `_emit_sweep` writes a `started` entry before the
    sweep runs and a `succeeded` entry after, both `component="api",
    action="change_check_sweep"`, both carrying the exact `run_id` passed
    into `run_change_check_sweep(run_id=...)` -- through a real `LogEmitter`,
    not a stub (mirrors `test_run_context.py`'s/
    `test_ingestion_orchestration.py`'s own established shape).
    """
    emitter, log_path = make_emitter()
    fake = build_fake_change_check_dependencies(tracked=())

    run_change_check_sweep(
        config=app_config, run_id="sweep-run-1", dependencies=fake.dependencies, emitter=emitter
    )
    emitter.flush()

    sweep_lines = [
        line for line in read_lines(log_path) if line.get("action") == "change_check_sweep"
    ]
    assert [line["outcome"] for line in sweep_lines] == ["started", "succeeded"]
    assert all(line["component"] == "api" for line in sweep_lines)
    assert all(line["run_id"] == "sweep-run-1" for line in sweep_lines)


def test_sweep_emits_one_change_check_instrument_entry_per_tracked_instrument_carrying_the_run_id(
    app_config: ServiceConfig,
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    """AC-BI-008 (full, D8): 3 tracked instruments, mixed poll-stage buckets ->
    3 `component="api", action="change_check_instrument"` entries, each
    `entity_id` equal to its instrument id, each `outcome` matching the
    bucket it landed in, all three carrying the same sweep `run_id` -- not
    `None`, not a different id, the direct disproof of any accidental
    reliance on `poll_for_amendments`'s own separately-minted nested id.
    """
    emitter, log_path = make_emitter()
    tracked = (_node("id_a"), _node("id_b"), _node("id_c"))
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(
            findings=(), polled_count=3, failed_ids=("id_b",), unconfigured_ids=("id_c",)
        ),
    )

    run_change_check_sweep(
        config=app_config, run_id="sweep-run-2", dependencies=fake.dependencies, emitter=emitter
    )
    emitter.flush()

    instrument_lines = [
        line for line in read_lines(log_path) if line.get("action") == "change_check_instrument"
    ]
    assert len(instrument_lines) == 3
    outcomes_by_id = {line["entity_id"]: line["outcome"] for line in instrument_lines}
    assert outcomes_by_id == {
        "id_a": "current",
        "id_b": "poll_failed",
        "id_c": "not_configured",
    }
    assert all(line["component"] == "api" for line in instrument_lines)
    assert all(line["run_id"] == "sweep-run-2" for line in instrument_lines)


# --- Slice 7: AC-BI-009 dedicated proof (exactly-once processing, no fan-out) ---


def test_sweep_calls_trigger_reingestion_at_most_once_per_tracked_instrument_id(
    app_config: ServiceConfig,
    make_emitter: MakeEmitter,
) -> None:
    """5 tracked instruments, 2 with findings -> `trigger_reingestion` is
    called exactly twice, once per finding's own tracked instrument
    (identified here by each instrument's own distinct `celex`, the value
    D4 says is passed as `identifier`) -- no duplicates, and no identifier
    outside the two finding instruments' own celex values.
    """
    emitter, _ = make_emitter()

    def _tracked_node(instrument_id: str, celex: str) -> TrackedInstrumentNode:
        return TrackedInstrumentNode(
            regulatory_instrument_id=instrument_id,
            celex=celex,
            instrument_type="regulation",
            effective_date="2024-01-01",
        )

    tracked = (
        _tracked_node("A", "10000001"),
        _tracked_node("B", "10000002"),
        _tracked_node("C", "10000003"),
        _tracked_node("D", "10000004"),
        _tracked_node("E", "10000005"),
    )
    entries = {
        "10000002": CatalogEntry(celex="10000002", title="B Act", short_name="B", version="1.0"),
        "10000004": CatalogEntry(celex="10000004", title="D Act", short_name="D", version="1.0"),
    }
    reingestion_success = ReingestionOutcome(
        prior_regulatory_instrument_id="prior",
        new_regulatory_instrument_id="new",
        run_id="ingest-run",
        outcome="superseded",
        ingest_counts={},
    )
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(
            findings=(_amendment_finding("B"), _amendment_finding("D")),
            polled_count=5,
            failed_ids=(),
            unconfigured_ids=(),
        ),
        catalog_entries=entries,
        reingestion_results=(reingestion_success, reingestion_success),
    )

    run_change_check_sweep(
        config=app_config, run_id="r1", dependencies=fake.dependencies, emitter=emitter
    )

    identifiers = [call.identifier for call in fake.trigger_reingestion_calls]
    assert len(fake.trigger_reingestion_calls) == 2
    assert sorted(identifiers) == ["10000002", "10000004"]
    assert len(set(identifiers)) == len(identifiers)


def test_sweep_never_calls_poll_for_amendments_more_than_once(
    app_config: ServiceConfig,
    make_emitter: MakeEmitter,
) -> None:
    """`poll_for_amendments`'s call count is `1` regardless of how many
    findings/failures the scripted `PollReport` contains -- disproves any
    accidental re-poll-after-reingest loop-back.
    """
    emitter, _ = make_emitter()
    tracked = (_node("A"), _node("B"), _node("C"), _node("D"))
    entry = CatalogEntry(
        celex="32024R2847", title="Cyber Resilience Act", short_name="CRA", version="1.0"
    )
    reingestion_success = ReingestionOutcome(
        prior_regulatory_instrument_id="prior",
        new_regulatory_instrument_id="new",
        run_id="ingest-run",
        outcome="superseded",
        ingest_counts={},
    )
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(
            findings=(_amendment_finding("A"),),
            polled_count=4,
            failed_ids=("B",),
            unconfigured_ids=("C",),
        ),
        catalog_entries={"32024R2847": entry},
        reingestion_results=(reingestion_success,),
    )

    run_change_check_sweep(
        config=app_config, run_id="r1", dependencies=fake.dependencies, emitter=emitter
    )

    assert len(fake.poll_for_amendments_graphs) == 1


def test_sweep_result_instrument_count_equals_read_tracked_instruments_count_exactly(
    app_config: ServiceConfig,
    make_emitter: MakeEmitter,
) -> None:
    """4 tracked instruments, any mix of buckets -> `len(result.instruments)
    == 4` (no dropped instrument, no duplicated/fan-out instrument), and the
    returned order matches `read_tracked_instruments`'s own returned order
    (no reshuffling that could hide a duplicate).
    """
    emitter, _ = make_emitter()
    tracked = (_node("A"), _node("B"), _node("C"), _node("D"))
    entry = CatalogEntry(
        celex="32024R2847", title="Cyber Resilience Act", short_name="CRA", version="1.0"
    )
    reingestion_success = ReingestionOutcome(
        prior_regulatory_instrument_id="prior",
        new_regulatory_instrument_id="new",
        run_id="ingest-run",
        outcome="superseded",
        ingest_counts={},
    )
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(
            findings=(_amendment_finding("A"),),
            polled_count=4,
            failed_ids=("B",),
            unconfigured_ids=("C",),
        ),
        catalog_entries={"32024R2847": entry},
        reingestion_results=(reingestion_success,),
    )

    result = run_change_check_sweep(
        config=app_config, run_id="r1", dependencies=fake.dependencies, emitter=emitter
    )

    assert len(result.instruments) == 4
    assert [o.instrument_id for o in result.instruments] == ["A", "B", "C", "D"]
