"""Tests for ``ps_service.api.run_status`` (the process-wide run-stage registry).

Increment 10 (#61, AC-BI-008 foundation). Mirrors
``tests/dependency_health/test_registry.py`` in shape: no real pipeline, no
FastAPI app -- just the module-level ``dict`` + lock under direct test.
"""

from __future__ import annotations

from ps_service.api.run_status import clear_stage, get_stage, reset_for_tests, set_stage


def test_set_stage_then_get_stage_returns_the_recorded_stage() -> None:
    set_stage("run-1", "ingestion")

    assert get_stage("run-1") == "ingestion"


def test_get_stage_returns_none_for_unknown_run_id() -> None:
    assert get_stage("no-such-run") is None


def test_clear_stage_removes_the_entry() -> None:
    set_stage("run-2", "extraction")

    clear_stage("run-2")

    assert get_stage("run-2") is None


def test_set_stage_overwrites_previous_value_for_same_run_id() -> None:
    set_stage("run-3", "ingestion")
    set_stage("run-3", "derivation")

    assert get_stage("run-3") == "derivation"


def test_reset_for_tests_clears_all_entries() -> None:
    set_stage("run-4", "ingestion")
    set_stage("run-5", "merge")

    reset_for_tests()

    assert get_stage("run-4") is None
    assert get_stage("run-5") is None
