"""Tests for ps_service.logging.run_context.bind_run_context/current_run_id (AC#1)."""

from __future__ import annotations

import uuid

from ps_service.logging import bind_run_context, current_run_id


def test_run_id_when_bound_then_downstream_reads_see_it() -> None:
    with bind_run_context("run-abc"):

        def downstream_read() -> str | None:
            return current_run_id()

        assert downstream_read() == "run-abc"
    assert current_run_id() is None


def test_run_id_when_generated_then_is_valid_uuid4() -> None:
    with bind_run_context() as generated:
        assert current_run_id() == generated
        parsed = uuid.UUID(generated)
        assert parsed.version == 4


def test_nested_binding_when_inner_exits_then_restores_outer_run_id() -> None:
    with bind_run_context("A"):
        assert current_run_id() == "A"
        with bind_run_context("B"):
            assert current_run_id() == "B"
        assert current_run_id() == "A"  # G1 fix: restored, not deleted
    assert current_run_id() is None


def test_bind_run_context_when_no_prior_binding_then_unbinds_cleanly_on_exit() -> None:
    assert current_run_id() is None
    with bind_run_context("solo"):
        assert current_run_id() == "solo"
    assert current_run_id() is None
