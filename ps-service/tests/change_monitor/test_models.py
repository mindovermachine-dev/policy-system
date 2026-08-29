"""Tests for `ps_service.change_monitor.models`."""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import TYPE_CHECKING

import pytest

from ps_service.change_monitor.models import (
    AmendmentFinding,
    PollReport,
    ReingestionOutcome,
    TrackedInstrumentNode,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _mutate(obj: object, attr: str, value: object) -> None:
    """Write ``obj.attr = value`` through a dynamic path.

    The frozen-dataclass ``__setattr__`` guard -- not the type checker -- is
    what must reject the write, so the assignment target is kept off the
    statically-known attribute path (basedpyright strict would otherwise
    flag every one of these as ``reportAttributeAccessIssue``).
    """
    setattr(obj, attr, value)


def _tracked_instrument_node() -> TrackedInstrumentNode:
    return TrackedInstrumentNode(
        regulatory_instrument_id="CRA-1.0",
        celex="32024R2847",
        instrument_type="regulation",
        effective_date="2027-12-11",
    )


def _amendment_finding() -> AmendmentFinding:
    return AmendmentFinding(
        regulatory_instrument_id="CRA-1.0",
        instrument_type="regulation",
        baseline_reference="2027-12-11",
        detected_consolidated_celex="02024R2847-20241120",
        detected_consolidation_date=date(2024, 11, 20),
        reason="newer_consolidation",
    )


def _poll_report() -> PollReport:
    return PollReport(
        findings=(_amendment_finding(),),
        polled_count=3,
        failed_ids=("NIS2-1.0",),
        unconfigured_ids=("DORA-1.0",),
    )


def _reingestion_outcome() -> ReingestionOutcome:
    return ReingestionOutcome(
        prior_regulatory_instrument_id="CRA-1.0",
        new_regulatory_instrument_id="CRA-2.0",
        run_id="run-abc",
        outcome="superseded",
        ingest_counts=None,
    )


_FACTORIES: list[Callable[[], object]] = [
    _tracked_instrument_node,
    _amendment_finding,
    _poll_report,
    _reingestion_outcome,
]


@pytest.mark.parametrize("factory", _FACTORIES)
def test_every_model_is_a_frozen_dataclass(factory: Callable[[], object]) -> None:
    instance = factory()

    assert dataclasses.is_dataclass(instance)
    assert dataclasses.fields(instance)  # has at least one field
    with pytest.raises(dataclasses.FrozenInstanceError):
        _mutate(instance, dataclasses.fields(instance)[0].name, "changed")


@pytest.mark.parametrize("factory", _FACTORIES)
def test_every_model_uses_slots_and_has_no_instance_dict(
    factory: Callable[[], object],
) -> None:
    instance = factory()

    assert hasattr(type(instance), "__slots__")
    assert not hasattr(instance, "__dict__")


def test_tracked_instrument_node_carries_only_the_projected_fields() -> None:
    node = _tracked_instrument_node()

    assert node.regulatory_instrument_id == "CRA-1.0"
    assert node.celex == "32024R2847"
    assert node.instrument_type == "regulation"
    assert node.effective_date == "2027-12-11"


def test_tracked_instrument_node_allows_null_celex() -> None:
    node = TrackedInstrumentNode(
        regulatory_instrument_id="LEGACY-1.0",
        celex=None,
        instrument_type="directive",
        effective_date="2020-01-01",
    )

    assert node.celex is None


def test_amendment_finding_reason_carries_the_classification() -> None:
    finding = dataclasses.replace(_amendment_finding(), reason="baseline_unknown")

    assert finding.reason == "baseline_unknown"
    assert finding.detected_consolidation_date == date(2024, 11, 20)


def test_poll_report_tuples_and_counts_are_readable() -> None:
    report = _poll_report()

    assert report.polled_count == 3
    assert report.findings[0].regulatory_instrument_id == "CRA-1.0"
    assert report.failed_ids == ("NIS2-1.0",)
    assert report.unconfigured_ids == ("DORA-1.0",)


def test_reingestion_outcome_allows_null_run_id_and_counts() -> None:
    outcome = ReingestionOutcome(
        prior_regulatory_instrument_id="CRA-1.0",
        new_regulatory_instrument_id="CRA-2.0",
        run_id=None,
        outcome="already_processed",
        ingest_counts=None,
    )

    assert outcome.run_id is None
    assert outcome.ingest_counts is None
    assert outcome.outcome == "already_processed"
