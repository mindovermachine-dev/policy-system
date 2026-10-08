"""Tests for the `get_ingestion_status` MCP tool (issue #194, S2 and S5).

S5 covers failure outcomes: a failed run reports the same named `error:` text
`ingest_regulation` returns, an unexpected exception is sanitized, and a run that is left
`running` with no worker (a failed terminal write, a process restart) is reconciled to `failed`
on the next poll rather than reported `running` forever.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from api._fakes import build_fake_pipeline_dependencies
from mcp.server.mcpserver.exceptions import ToolError

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
    _CELEX,  # pyright: ignore[reportPrivateUsage]  -- reuse the curated-catalog fixture verbatim, mirrors `test_start_ingestion_tool.py`
    _SHORT_NAME,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _call_ingest_regulation,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _configure_complete_llm_env,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _text,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _use_fake_graph_openers_only,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _use_real_pipeline_stages,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.ingestion_runs import IngestionRunRow, dispatch
from ps_service.logging import configure, resolve_default_log_path
from ps_service.mcp_interface import mcp_server

# The sync tool writes `ingestion_run.*` rows (issue #195): keep them off Postgres.
pytestmark = pytest.mark.usefixtures("ingest_audit_store")

_RECONCILER = ("system:ingestion-run-reconciler", "system:ingestion-run-reconciler")
_INTERRUPTED_RUN_MESSAGE = (
    "error: the ingestion run was interrupted before it finished; its outcome is unknown"
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from api._fakes import ReadLines
    from ingestion_runs._fakes import InMemoryIngestionRunStore


@pytest.fixture(autouse=True)
def _isolate(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture
) -> Iterator[None]:
    isolate_ingestion_runs()
    yield
    isolate_ingestion_runs()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryIngestionRunStore:
    """Bypass on, logging configured, run store faked."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    return install_ingestion_run_store(monkeypatch)


@pytest.mark.parametrize("run_id", ["3f2b8c1e-5d4a-4e7b-9a60-1c2d3e4f5a6b", "not-a-uuid", "x"])
def test_a_never_submitted_run_id_returns_the_unknown_shape(
    run_id: str, store: InMemoryIngestionRunStore
) -> None:
    """AC-BI-004: unknown (or unparseable) ids answer `unknown`, never raise."""
    _ = store

    assert body(call_get_ingestion_status(run_id)) == {
        "run_id": run_id,
        "status": "unknown",
        "stage": None,
        "result": None,
        "error": None,
    }


@pytest.mark.parametrize("run_id", ["", "a" * 65])
def test_an_out_of_range_run_id_is_rejected_by_the_schema(
    run_id: str, store: InMemoryIngestionRunStore
) -> None:
    _ = store

    with pytest.raises(ToolError):
        call_get_ingestion_status(run_id)


def test_a_store_outage_returns_the_fixed_unavailable_error(
    store: InMemoryIngestionRunStore,
) -> None:
    store.unavailable = True

    assert (
        text(call_get_ingestion_status(str(uuid.uuid4())))
        == "error: The ingestion run store is temporarily unavailable."
    )


# --- S5: failure outcomes, sanitization, never stuck at "running" -----------------------------


@pytest.fixture
def pipeline_store(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryIngestionRunStore
) -> InMemoryIngestionRunStore:
    """`store` plus the complete LLM env the synchronous pre-flight needs."""
    _configure_complete_llm_env(monkeypatch)
    return store


def _submit_and_finish() -> str:
    run_id = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])
    wait_for_run(run_id)
    return run_id


def _status(run_id: str) -> dict[str, object]:
    return body(call_get_ingestion_status(run_id))


def _background_outcomes(read_lines: ReadLines, run_id: str) -> list[object]:
    return [
        line["outcome"]
        for line in read_lines(resolve_default_log_path())
        if line.get("component") == "mcp_interface"
        and line.get("action") == "background_ingestion_run"
        and line.get("run_id") == run_id
    ]


def _seed_running_row(store: InMemoryIngestionRunStore) -> str:
    """A `running` row no in-process worker holds, as left behind by a restarted process."""
    run_id = str(uuid.uuid4())
    store.rows[run_id] = IngestionRunRow(
        run_id=run_id,
        celex=_CELEX,
        short_name=_SHORT_NAME,
        actor_subject="system:local-test-bypass",
        actor_issuer="system:local-test-bypass",
        status="running",
        result=None,
        error=None,
        submitted_at=datetime.now(UTC),
        finished_at=None,
    )
    return run_id


def test_a_mid_run_pipeline_failure_polls_as_the_same_error_ingest_regulation_returns(
    monkeypatch: pytest.MonkeyPatch,
    pipeline_store: InMemoryIngestionRunStore,
    read_lines: ReadLines,
) -> None:
    """AC-BI-013: `failed`, no result, the exact named error the blocking tool returns."""
    _ = pipeline_store
    emitter = configure()
    _use_real_pipeline_stages(
        monkeypatch, extract_error=RuntimeError("boom -- must never reach the caller")
    )

    run_id = _submit_and_finish()
    polled = _status(run_id)

    assert polled == {
        "run_id": run_id,
        "status": "failed",
        "stage": None,
        "result": None,
        "error": "error: extraction stage failed: extraction failed",
    }
    _use_real_pipeline_stages(
        monkeypatch, extract_error=RuntimeError("boom -- must never reach the caller")
    )
    assert polled["error"] == _text(_call_ingest_regulation(_CELEX, _SHORT_NAME))
    emitter.flush()
    assert _background_outcomes(read_lines, run_id) == ["started", "failed"]
    assert "boom" not in json.dumps(polled)


def test_a_graph_unreachable_mid_run_polls_as_the_fixed_unavailable_error(
    monkeypatch: pytest.MonkeyPatch, pipeline_store: InMemoryIngestionRunStore
) -> None:
    """AC-BI-013/015: a connection failure on the native graph never leaks host detail."""
    _ = pipeline_store
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")

    def _raising_native(config: object, short_name: str) -> object:
        _ = (config, short_name)
        message = "connection refused to 10.0.0.1:6379"  # must never reach the caller
        raise ConnectionError(message)

    _use_fake_graph_openers_only(
        monkeypatch, dataclasses.replace(fake.dependencies.graphs, native=_raising_native)
    )

    polled = _status(_submit_and_finish())

    assert polled["status"] == "failed"
    assert polled["result"] is None
    assert polled["error"] == "error: the policy graph database is not reachable"
    assert "10.0.0.1" not in json.dumps(polled)


def test_an_unexpected_background_exception_is_sanitized_but_logged_server_side(
    monkeypatch: pytest.MonkeyPatch,
    pipeline_store: InMemoryIngestionRunStore,
    read_lines: ReadLines,
) -> None:
    """AC-BI-014/015: the row is `failed` with the generic text; detail stays in the log."""
    _ = pipeline_store
    emitter = configure()
    _use_real_pipeline_stages(monkeypatch)

    def _boom(run_id: str, outcome: object) -> object:
        _ = (run_id, outcome)
        raise RuntimeError("boom secret 10.0.0.1:6379 /etc/x")

    # detroit-exception: `_to_accepted_response` is a pure, always-succeeding response shaper
    # for any well-formed outcome, so no real input reaches the sanitizing safety net; forcing
    # it is the same fault-injection carve-out as `test_ingest_regulation_tool.py`'s
    # `_to_accepted_response` test (D-AUDIT-WRAPPER point 4).
    monkeypatch.setattr(mcp_server, "_to_accepted_response", _boom)

    run_id = _submit_and_finish()
    polled = _status(run_id)

    assert polled["status"] == "failed"
    assert polled["error"] == "error: an unexpected error occurred"
    dumped = json.dumps(polled)
    for secret in ("boom", "10.0.0.1", "/etc/x", "Traceback"):
        assert secret not in dumped
    emitter.flush()
    failed = [
        line
        for line in read_lines(resolve_default_log_path())
        if line.get("action") == "background_ingestion_run"
        and line.get("run_id") == run_id
        and line.get("outcome") == "failed"
    ]
    assert len(failed) == 1
    assert "RuntimeError" in str(failed[0].get("detail"))
    assert "boom" in str(failed[0].get("detail"))


def test_a_failed_terminal_write_is_reconciled_to_failed_on_the_next_poll(
    monkeypatch: pytest.MonkeyPatch, pipeline_store: InMemoryIngestionRunStore
) -> None:
    """AC-BI-014 / AC-BI-008: a lost terminal write never leaves the run `running` forever."""
    _use_real_pipeline_stages(monkeypatch)
    pipeline_store.fail_next_complete = 1

    run_id = _submit_and_finish()

    assert pipeline_store.rows[run_id].status == "running"
    assert not dispatch.is_run_in_flight(run_id)

    first = _status(run_id)

    assert first["status"] == "failed"
    assert first["error"] == _INTERRUPTED_RUN_MESSAGE
    assert first["result"] is None
    assert pipeline_store.rows[run_id].finished_at is not None
    assert _status(run_id) == first
    assert pipeline_store.complete_wins[run_id] == 1


def test_a_run_row_left_running_by_a_restarted_process_polls_as_interrupted(
    pipeline_store: InMemoryIngestionRunStore,
) -> None:
    """AC-BI-014: a `running` row with no worker in this process is reported interrupted."""
    run_id = _seed_running_row(pipeline_store)

    assert _status(run_id) == {
        "run_id": run_id,
        "status": "failed",
        "stage": None,
        "result": None,
        "error": _INTERRUPTED_RUN_MESSAGE,
    }


def test_polling_a_run_that_is_still_in_flight_never_reconciles_it(
    monkeypatch: pytest.MonkeyPatch, pipeline_store: InMemoryIngestionRunStore
) -> None:
    """The reconciliation race is a no-op: a live worker's run is `running`, then `succeeded`."""
    pipeline = use_gated_real_pipeline(monkeypatch)
    try:
        run_id = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])

        assert _status(run_id)["status"] == "running"
        assert pipeline_store.complete_wins == {}
    finally:
        pipeline.gate.set()
    wait_for_run(run_id)

    assert _status(run_id)["status"] == "succeeded"
    assert pipeline_store.complete_wins[run_id] == 1


def test_a_failed_reconciliation_write_leaves_the_row_running_and_never_fails_the_poll(
    monkeypatch: pytest.MonkeyPatch,
    pipeline_store: InMemoryIngestionRunStore,
    read_lines: ReadLines,
) -> None:
    """MAJ-3(c) / MIN-6: both the worker's write and the first reconciliation fail.

    The first poll answers the row unchanged (`running`, no error, not an `error:` string)
    and logs the failure by exception class; the second poll reconciles it.
    """
    emitter = configure()
    _use_real_pipeline_stages(monkeypatch)
    pipeline_store.fail_next_complete = 2
    run_id = _submit_and_finish()

    first = _status(run_id)

    assert first["status"] == "running"
    assert first["error"] is None
    emitter.flush()
    logged = [
        line
        for line in read_lines(resolve_default_log_path())
        if line.get("action") == "ingestion_run_reconcile" and line.get("run_id") == run_id
    ]
    assert [(line["outcome"], line.get("reason")) for line in logged] == [
        ("failed", "IngestionRunPersistenceError")
    ]
    second = _status(run_id)
    assert second["status"] == "failed"
    assert second["error"] == _INTERRUPTED_RUN_MESSAGE


def test_reconciling_an_orphaned_run_writes_one_complete_row_under_reconciler_reason_interrupted(
    monkeypatch: pytest.MonkeyPatch, pipeline_store: InMemoryIngestionRunStore
) -> None:
    """OQ-7 / A6: the worker's lost write is reconciled by the poll, attributed to the sentinel."""
    _use_real_pipeline_stages(monkeypatch)
    pipeline_store.fail_next_complete = 1
    run_id = _submit_and_finish()

    _status(run_id)
    _status(run_id)

    completions = [
        e for e in pipeline_store.audit_entries if e.entry.action == "ingestion_run.complete"
    ]
    assert len(completions) == 1
    (completion,) = completions
    assert completion.resource_id == run_id
    assert completion.actor == _RECONCILER
    assert completion.entry.outcome == "failed"
    assert completion.entry.details == {
        "status": "failed",
        "celex": _CELEX,
        "trigger": "async_ingest",
        "reason_code": "interrupted",
        "new_obligations": 0,
        "new_capabilities": 0,
        "matched_capabilities": 0,
    }
    submission = next(
        e for e in pipeline_store.audit_entries if e.entry.action == "ingestion_run.submit"
    )
    assert submission.actor != _RECONCILER


def test_a_workers_own_completion_keeps_the_submitters_attribution(
    monkeypatch: pytest.MonkeyPatch, pipeline_store: InMemoryIngestionRunStore
) -> None:
    _use_real_pipeline_stages(monkeypatch)

    run_id = _submit_and_finish()

    (completion,) = [
        e for e in pipeline_store.audit_entries if e.entry.action == "ingestion_run.complete"
    ]
    assert completion.resource_id == run_id
    assert completion.actor == ("system:local-test-bypass", "system:local-test-bypass")


def test_polling_a_running_or_finished_run_writes_no_audit_row(
    monkeypatch: pytest.MonkeyPatch, pipeline_store: InMemoryIngestionRunStore
) -> None:
    """AC-BI-014: polls are write-free for a live and for a finished run."""
    pipeline = use_gated_real_pipeline(monkeypatch)
    try:
        run_id = str(body(call_start_ingestion(_CELEX, _SHORT_NAME))["run_id"])
        before = len(pipeline_store.audit_entries)
        _status(run_id)
        _status(run_id)
        assert len(pipeline_store.audit_entries) == before == 1
    finally:
        pipeline.gate.set()
    wait_for_run(run_id)
    finished = len(pipeline_store.audit_entries)

    _status(run_id)
    _status(run_id)

    assert finished == 2
    assert len(pipeline_store.audit_entries) == finished


def test_two_polls_of_an_orphaned_run_still_yield_one_complete_row(
    monkeypatch: pytest.MonkeyPatch, pipeline_store: InMemoryIngestionRunStore
) -> None:
    _use_real_pipeline_stages(monkeypatch)
    pipeline_store.fail_next_complete = 1
    run_id = _submit_and_finish()

    _status(run_id)
    _status(run_id)
    _status(run_id)

    completions = [
        e for e in pipeline_store.audit_entries if e.entry.action == "ingestion_run.complete"
    ]
    assert len(completions) == 1
    assert completions[0].resource_id == run_id
