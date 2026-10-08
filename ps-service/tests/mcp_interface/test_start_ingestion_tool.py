"""Tests for the `start_ingestion` MCP tool (issue #194, S2).

The tool validates synchronously, records an `ingestion_runs` row, dispatches the pipeline on a
background thread and returns a `run_id` at once. `get_ingestion_status` is used to poll the run
to its terminal result. Only genuine boundaries are faked: the run store (the approved Postgres
boundary), the graphs and the adapters/LLM callers behind `_use_real_pipeline_stages`.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import uuid
from typing import TYPE_CHECKING, cast

import pytest
from authz._fakes import (
    FakeAccessRoleStore,  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors `test_ingestion_run_tools_authz_gate.py`
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from mcp_interface._ingestion_run_harness import (
    body,
    call_get_ingestion_status,
    call_start_ingestion,
    install_ingestion_run_store,
    isolate_ingestion_runs,
    text,
    use_gated_real_pipeline,
    wait_for_run,
)
from mcp_interface.test_ingest_regulation_tool import (
    _CELEX,  # pyright: ignore[reportPrivateUsage]  -- reuse the curated-catalog fixture verbatim, mirrors `test_ingest_regulation_authz_gate.py`
    _RID,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _SHORT_NAME,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _call_ingest_regulation,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _configure_complete_llm_env,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _text,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _use_real_pipeline_stages,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.api import run_status
from ps_service.api.catalog import REGULATION_CATALOG
from ps_service.authz.models import AccessRole
from ps_service.config import LOCAL_TEST_PRINCIPAL_ID
from ps_service.ingestion_runs import dispatch
from ps_service.logging import configure, resolve_default_log_path
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from api._fakes import ReadLines
    from ingestion_runs._fakes import InMemoryIngestionRunStore

_BYPASS_ACTOR = ("system:local-test-bypass", "system:local-test-bypass")


@pytest.fixture(autouse=True)
def _isolate(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture
) -> Iterator[None]:
    isolate_ingestion_runs()
    yield
    isolate_ingestion_runs()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryIngestionRunStore:
    """Bypass on, complete LLM env, logging configured, run store faked."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _configure_complete_llm_env(monkeypatch)
    configure()
    return install_ingestion_run_store(monkeypatch)


def _status(run_id: str) -> dict[str, object]:
    return body(call_get_ingestion_status(run_id))


def test_happy_path_returns_a_run_id_at_once_and_polls_to_the_ingestion_result(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    """AC-BI-005 / AC-BI-007: immediate `{run_id, status}`, then the blocking result shape."""
    _ = store
    _use_real_pipeline_stages(monkeypatch)

    started = body(call_start_ingestion(_CELEX, _SHORT_NAME))

    assert set(started) == {"run_id", "status"}
    assert started["status"] == "running"
    run_id = str(started["run_id"])
    wait_for_run(run_id)
    polled = _status(run_id)
    assert set(polled) == {"run_id", "status", "stage", "result", "error"}
    assert polled["status"] == "succeeded"
    assert polled["stage"] is None
    assert polled["error"] is None
    result = cast("dict[str, object]", polled["result"])
    assert isinstance(result, dict)
    assert set(result) == {"run_id", "regulatory_instrument_id", "source", "outcome", "stages"}
    assert result["run_id"] == run_id
    assert result["regulatory_instrument_id"] == _RID
    assert result["source"] == "catalog"
    assert result["outcome"] == "fresh"
    stages = cast("list[dict[str, object]]", result["stages"])
    assert [stage["stage"] for stage in stages] == [
        "ingestion",
        "extraction",
        "derivation",
        "merge",
    ]
    assert all(stage["status"] == "succeeded" for stage in stages)


def test_polled_result_equals_the_blocking_tools_response_apart_from_run_id(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    """AC-BI-007 / AC-BI-009: same outcome as `ingest_regulation`, apart from `run_id`."""
    _ = store
    _use_real_pipeline_stages(monkeypatch)
    started = body(call_start_ingestion(_CELEX, _SHORT_NAME))
    run_id = str(started["run_id"])
    wait_for_run(run_id)
    async_result = _status(run_id)["result"]
    assert isinstance(async_result, dict)
    async_result = cast("dict[str, object]", async_result)

    _use_real_pipeline_stages(monkeypatch)
    blocking = json.loads(_text(_call_ingest_regulation(_CELEX, _SHORT_NAME)))

    assert {k: v for k, v in async_result.items() if k != "run_id"} == {
        k: v for k, v in blocking.items() if k != "run_id"
    }


def test_a_row_is_recorded_for_the_submission_before_the_call_returns(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    """AC-BI-003: exactly one `ingestion_runs` row, under the returned id and the bypass actor."""
    _use_real_pipeline_stages(monkeypatch)

    started = body(call_start_ingestion(_CELEX, _SHORT_NAME))

    assert list(store.rows) == [started["run_id"]]
    row = store.rows[str(started["run_id"])]
    # Issue #193: the recorded short_name is the normalized (upper-case) one.
    assert (row.celex, row.short_name) == (_CELEX, _SHORT_NAME.upper())
    assert (row.actor_subject, row.actor_issuer) == _BYPASS_ACTOR


def test_the_pipeline_receives_the_submitted_run_id_celex_and_caller(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore, read_lines: ReadLines
) -> None:
    """AC-BI-009: the pipeline gets the same plain arguments as in the blocking tool."""
    _ = store
    emitter = configure()
    _use_real_pipeline_stages(monkeypatch)

    run_id = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])
    wait_for_run(run_id)

    emitter.flush()
    lines = [
        line
        for line in read_lines(resolve_default_log_path())
        if line.get("component") == "api" and line.get("action") == "ingestion_run"
    ]
    assert lines
    for line in lines:
        assert line["run_id"] == run_id
        assert line["source_identifier"] == _CELEX
        assert line["caller"] == LOCAL_TEST_PRINCIPAL_ID


def test_submission_and_background_run_log_one_correlated_lifecycle_each(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore, read_lines: ReadLines
) -> None:
    """Both the submitting call and the background run log started/succeeded under one run_id."""
    _ = store
    emitter = configure()
    _use_real_pipeline_stages(monkeypatch)

    run_id = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])
    wait_for_run(run_id)

    emitter.flush()
    lines = read_lines(resolve_default_log_path())
    for action in ("start_ingestion", "background_ingestion_run"):
        action_lines = [
            line
            for line in lines
            if line.get("component") == "mcp_interface" and line.get("action") == action
        ]
        assert [line["outcome"] for line in action_lines] == ["started", "succeeded"], action
        assert {line["run_id"] for line in action_lines} == {run_id}, action


def test_run_ids_are_random_uuid4_values_that_differ_between_submissions(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    """AC-BI-020: run ids are not sequential or guessable."""
    _ = store
    _use_real_pipeline_stages(monkeypatch)

    other_celex, other_short_name = _second_curated_entry()
    first = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])
    wait_for_run(first)
    second = str(body(call_start_ingestion(other_celex, other_short_name))["run_id"])
    wait_for_run(second)

    for run_id in (first, second):
        assert uuid.UUID(run_id).version == 4
        assert str(uuid.UUID(run_id)) == run_id
    assert first != second


def test_resubmitting_an_ingested_celex_is_rejected_before_any_row_or_thread(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    """Issue #193: an already-ingested CELEX is rejected, as `ingest_regulation` does."""
    _use_real_pipeline_stages(monkeypatch)
    first = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])
    wait_for_run(first)

    rejected = text(call_start_ingestion(_CELEX, _SHORT_NAME))

    assert (
        rejected
        == f"error: CELEX {_CELEX} is already ingested as short_name '{_SHORT_NAME.upper()}'"
    )
    assert set(store.rows) == {first}
    assert dispatch.in_flight_run_count() == 0


def test_a_slot_is_held_only_while_the_run_is_in_flight(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    _ = store
    _use_real_pipeline_stages(monkeypatch)

    run_id = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])
    wait_for_run(run_id)

    assert dispatch.in_flight_run_count() == 0
    assert text(call_get_ingestion_status(run_id)).startswith("{")


def test_start_ingestion_returns_while_the_pipeline_is_still_running(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    """AC-BI-005 / AC-BI-003: the call returns with the worker provably parked mid-pipeline."""
    pipeline = use_gated_real_pipeline(monkeypatch)
    try:
        started = body(call_start_ingestion(_CELEX, _SHORT_NAME))
        run_id = str(started["run_id"])

        assert started == {"run_id": run_id, "status": "running"}
        assert dispatch.is_run_in_flight(run_id)
        row = store.rows[run_id]
        assert row.status == "running"
        assert row.finished_at is None
    finally:
        pipeline.gate.set()
    wait_for_run(run_id)


def test_polling_a_running_run_reports_the_live_stage_then_clears_it(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    """AC-BI-006: running + current stage while parked; succeeded + no stage afterwards."""
    _ = store
    pipeline = use_gated_real_pipeline(monkeypatch)
    try:
        run_id = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])
        deadline = time.monotonic() + 10
        status = _status(run_id)
        while status["stage"] != "extraction" and time.monotonic() < deadline:
            time.sleep(0.01)
            status = _status(run_id)

        assert status == {
            "run_id": run_id,
            "status": "running",
            "stage": "extraction",
            "result": None,
            "error": None,
        }
        assert run_status.get_stage(run_id) == "extraction"
    finally:
        pipeline.gate.set()
    wait_for_run(run_id)

    done = _status(run_id)
    assert done["status"] == "succeeded"
    assert done["stage"] is None


# --- S5: submission failures release the slot and start nothing -------------------------------


def test_a_failed_row_write_at_submit_reports_it_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    """S5: the run could not be recorded, so nothing was started and the slot is free."""
    _use_real_pipeline_stages(monkeypatch)
    store.fail_next_create = 1

    result = text(call_start_ingestion(_CELEX, _SHORT_NAME))

    assert result == "error: The ingestion run could not be recorded."
    assert store.rows == {}
    assert dispatch.in_flight_run_count() == 0


def test_an_unavailable_run_store_at_submit_reports_it_and_releases_the_slot(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    _use_real_pipeline_stages(monkeypatch)
    store.unavailable = True

    result = text(call_start_ingestion(_CELEX, _SHORT_NAME))

    assert result == "error: The ingestion run store is temporarily unavailable."
    assert dispatch.in_flight_run_count() == 0


def test_an_unexpected_exception_at_submit_is_sanitized_and_releases_the_slot(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    """MIN-2: a non-store error from `create_run` gives the generic text and leaks no slot."""
    _use_real_pipeline_stages(monkeypatch)
    store.raise_on_create = RuntimeError("boom secret 10.0.0.1:6379")

    result = text(call_start_ingestion(_CELEX, _SHORT_NAME))

    assert result == "error: an unexpected error occurred"
    assert "boom" not in result
    assert store.rows == {}
    assert dispatch.in_flight_run_count() == 0


# --- S6: bounded admission and isolation between concurrent runs ------------------------------

_CAP_ERROR = (
    "error: too many ingestion runs are already in progress (limit 1); "
    "wait for one to finish, then try again"
)


def _second_curated_entry() -> tuple[str, str]:
    entry = next(e for e in REGULATION_CATALOG if e.celex != _CELEX)
    return entry.celex, entry.short_name


def _poll_until_stage(run_id: str, stage: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    status = _status(run_id)
    while status["stage"] != stage and time.monotonic() < deadline:
        time.sleep(0.01)
        status = _status(run_id)
    return status


def test_the_default_cap_admits_one_run_and_rejects_a_second_until_the_first_finishes(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    """AC-BI-012: over the cap -> the named rate-limit error; nothing recorded or spawned."""
    monkeypatch.delenv("PS_INGESTIONRUNS_MAX_IN_FLIGHT", raising=False)
    other_celex, other_short_name = _second_curated_entry()
    pipeline = use_gated_real_pipeline(monkeypatch, per_short_name_graphs=True)
    try:
        first = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])
        threads_before = threading.active_count()

        rejected = text(call_start_ingestion(other_celex, other_short_name))

        assert rejected == _CAP_ERROR
        assert set(store.rows) == {first}
        assert dispatch.in_flight_run_count() == 1
        assert threading.active_count() == threads_before
        pipeline.release(_SHORT_NAME)
        wait_for_run(first)

        third = body(call_start_ingestion(other_celex, other_short_name))
        pipeline.release(other_short_name)
        wait_for_run(str(third["run_id"]))
    finally:
        pipeline.release_all()

    assert third["status"] == "running"


def test_two_concurrent_runs_are_tracked_and_completed_independently(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    """AC-BI-019: distinct run ids, separate stage keys, separate rows and results."""
    monkeypatch.setenv("PS_INGESTIONRUNS_MAX_IN_FLIGHT", "2")
    other_celex, other_short_name = _second_curated_entry()
    pipeline = use_gated_real_pipeline(monkeypatch, per_short_name_graphs=True)
    try:
        run_a = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])
        run_b = str(body(call_start_ingestion(other_celex, other_short_name))["run_id"])
        assert run_a != run_b
        assert _poll_until_stage(run_a, "extraction")["status"] == "running"
        assert _poll_until_stage(run_b, "extraction")["status"] == "running"
        assert run_status.get_stage(run_a) == "extraction"
        assert run_status.get_stage(run_b) == "extraction"
        assert store.rows[run_a].celex == _CELEX
        assert store.rows[run_b].celex == other_celex

        pipeline.release(_SHORT_NAME)
        wait_for_run(run_a)

        done_a = _status(run_a)
        still_b = _status(run_b)
        assert done_a["status"] == "succeeded"
        assert still_b["status"] == "running"
        assert still_b["stage"] == "extraction"
        result_a = cast("dict[str, object]", done_a["result"])
        assert result_a["run_id"] == run_a
        assert result_a["regulatory_instrument_id"] == _RID

        pipeline.release(other_short_name)
        wait_for_run(run_b)

        done_b = _status(run_b)
        assert done_b["status"] == "succeeded"
        result_b = cast("dict[str, object]", done_b["result"])
        assert result_b["run_id"] == run_b
        assert result_b["regulatory_instrument_id"] != _RID
        assert str(result_b["regulatory_instrument_id"]).startswith(other_short_name.upper())
    finally:
        pipeline.release_all()


def test_each_concurrent_runs_background_log_lines_carry_only_its_own_run_id(
    monkeypatch: pytest.MonkeyPatch,
    store: InMemoryIngestionRunStore,
    read_lines: ReadLines,
) -> None:
    """AC-BI-019: log correlation does not bleed between simultaneous runs."""
    _ = store
    monkeypatch.setenv("PS_INGESTIONRUNS_MAX_IN_FLIGHT", "2")
    emitter = configure()
    other_celex, other_short_name = _second_curated_entry()
    pipeline = use_gated_real_pipeline(monkeypatch, per_short_name_graphs=True)
    try:
        run_a = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])
        run_b = str(body(call_start_ingestion(other_celex, other_short_name))["run_id"])
        _poll_until_stage(run_a, "extraction")
        _poll_until_stage(run_b, "extraction")
        pipeline.release_all()
        wait_for_run(run_a)
        wait_for_run(run_b)
    finally:
        pipeline.release_all()

    emitter.flush()
    lines = [
        line
        for line in read_lines(resolve_default_log_path())
        if line.get("component") == "mcp_interface"
        and line.get("action") == "background_ingestion_run"
    ]
    for run_id in (run_a, run_b):
        own = [line for line in lines if line["run_id"] == run_id]
        assert [line["outcome"] for line in own] == ["started", "succeeded"]
    assert len(lines) == 4


def test_resubmitting_a_short_name_that_is_in_flight_is_rejected_before_the_cap(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    """OQ-10: same short_name in flight -> the duplicate error, one row, one slot."""
    monkeypatch.setenv("PS_INGESTIONRUNS_MAX_IN_FLIGHT", "2")
    pipeline = use_gated_real_pipeline(monkeypatch)
    try:
        first = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])

        rejected = text(call_start_ingestion(_CELEX, _SHORT_NAME))

        assert rejected == (
            f"error: an ingestion run for short_name '{_SHORT_NAME.upper()}' "
            "is already in progress; "
            "wait for it to finish instead of submitting it again"
        )
        assert set(store.rows) == {first}
        assert dispatch.in_flight_run_count() == 1
    finally:
        pipeline.release_all()
    wait_for_run(first)


def test_the_duplicate_short_name_error_wins_over_the_cap_error(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    """OQ-10: with the default cap of 1 reached, a duplicate still gets the duplicate text."""
    _ = store
    monkeypatch.delenv("PS_INGESTIONRUNS_MAX_IN_FLIGHT", raising=False)
    pipeline = use_gated_real_pipeline(monkeypatch)
    try:
        first = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])

        rejected = text(call_start_ingestion(_CELEX, _SHORT_NAME))

        assert rejected.startswith("error: an ingestion run for short_name ")
    finally:
        pipeline.release_all()
    wait_for_run(first)


# --- S7: durable audit trail (AC-BI-016 / AC-BI-017) -------------------------------------------

_SUBJECT = "granted-compliance-officer"
_OWNER = "existing-system-owner"
_ISSUER = "https://issuer.example.com/"
_RECONCILER = ("system:ingestion-run-reconciler", "system:ingestion-run-reconciler")


@contextlib.contextmanager
def _verified_actor(*, sub: str, iss: str = _ISSUER) -> Generator[None]:
    """Bind a real, verified `AccessToken`; never sets the local-test bypass."""
    access_token = AccessToken(
        token="test-token", client_id="test-client", scopes=[], subject=sub, claims={"iss": iss}
    )
    token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(token)


def _grant_compliance_officer(monkeypatch: pytest.MonkeyPatch) -> None:
    access = FakeAccessRoleStore(expected_owner=(_OWNER, _ISSUER))
    access.bootstrap_first_owner((_OWNER, _ISSUER))
    access.grant(
        actor=(_OWNER, _ISSUER),
        target=(_SUBJECT, _ISSUER),
        access_role=AccessRole.COMPLIANCE_OFFICER,
    )

    def _factory(_config: object, **_kwargs: object) -> FakeAccessRoleStore:
        return access

    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _factory)


def test_the_submission_is_audited_once_under_the_verified_caller_with_status_started(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore, read_lines: ReadLines
) -> None:
    """AC-BI-016: the durable audit entry is the proof; the log triad is correlation only."""
    monkeypatch.delenv("PS_SERVICE_LOCAL_TEST_BYPASS")
    emitter = configure()
    _grant_compliance_officer(monkeypatch)
    _use_real_pipeline_stages(monkeypatch)

    with _verified_actor(sub=_SUBJECT):
        run_id = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])
    wait_for_run(run_id)

    submissions = [e for e in store.audit_entries if e.entry.action == "ingestion_run.submit"]
    assert len(submissions) == 1
    (submission,) = submissions
    assert submission.actor == (_SUBJECT, _ISSUER)
    assert submission.resource_id == run_id
    assert submission.entry.outcome == "applied"
    assert submission.entry.details == {
        "celex": _CELEX,
        "short_name": _SHORT_NAME.upper(),
        "status": "started",
    }
    # Correlation only (not proof): the operational log line carries the same run_id and caller.
    emitter.flush()
    started = [
        line
        for line in read_lines(resolve_default_log_path())
        if line.get("action") == "start_ingestion" and line.get("outcome") == "started"
    ]
    assert [(line["run_id"], line["principal"]) for line in started] == [(run_id, _SUBJECT)]


def test_the_submission_under_the_local_test_bypass_is_audited_as_the_bypass_actor(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    _use_real_pipeline_stages(monkeypatch)

    run_id = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])
    wait_for_run(run_id)

    submission = next(e for e in store.audit_entries if e.entry.action == "ingestion_run.submit")
    assert submission.actor == _BYPASS_ACTOR
    assert submission.resource_id == run_id


def test_completion_is_audited_once_from_the_workers_own_thread_under_the_submitter(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    """AC-BI-017: a second entry under the same run_id, written off the submitting thread."""
    _use_real_pipeline_stages(monkeypatch)

    run_id = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])
    wait_for_run(run_id)

    completions = [e for e in store.audit_entries if e.entry.action == "ingestion_run.complete"]
    assert len(completions) == 1
    (completion,) = completions
    assert completion.resource_id == run_id
    assert completion.actor == _BYPASS_ACTOR
    assert completion.entry.outcome == "applied"
    assert completion.entry.details["status"] == "succeeded"
    submission = next(e for e in store.audit_entries if e.entry.action == "ingestion_run.submit")
    # Written from the worker's own context, not the (still-open) submitting call's thread.
    assert completion.thread_ident not in {threading.get_ident(), submission.thread_ident}


def test_a_failed_run_is_audited_as_a_failed_completion_with_the_named_error(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    _use_real_pipeline_stages(monkeypatch, extract_error=RuntimeError("boom"))

    run_id = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])
    wait_for_run(run_id)

    (completion,) = [e for e in store.audit_entries if e.entry.action == "ingestion_run.complete"]
    assert completion.resource_id == run_id
    assert completion.entry.outcome == "failed"
    assert completion.entry.details == {
        "status": "failed",
        "error": "error: extraction stage failed: extraction failed",
    }


def test_a_rejected_submission_writes_no_audit_entry(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> None:
    store.fail_next_create = 1
    _use_real_pipeline_stages(monkeypatch)

    assert text(call_start_ingestion(_CELEX, _SHORT_NAME)).startswith("error: ")

    assert store.audit_entries == []
