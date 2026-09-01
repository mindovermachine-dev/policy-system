"""Tests for ps_cli.modules.handlers: handle_regulations_list (PLAN.md §3 Increment 9),
handle_regulations_ingest (PLAN.md §3 Increment 12).

`handle_internal_ingest` (PLAN.md §3 Increment 15) is deliberately not
unit-tested at this layer -- per PLAN.md §3 Increment 15 and the batch task
brief, its coverage is exactly two `cli.run()`-level tests in
`ps-cli/tests/test_cli.py`, which prove the full wiring (parser -> dispatch ->
handler -> client) end to end.
"""

from __future__ import annotations

import threading
import time

import pytest

from ps_cli.errors import PsCliError
from ps_cli.models import IngestionResult, RegulationEntry, RegulationsResult, StageOutcome
from ps_cli.modules.handlers import handle_regulations_ingest, handle_regulations_list


class _UnusedPsServiceClientMethods:
    """Base for hand-written `PsServiceClientProtocol` fakes below (PLAN.md §1 D10).

    Every method raises unless a subclass overrides it -- a fake overrides
    only the method its own test actually exercises, so an unexpected call
    to any other method fails loudly and immediately, the same guarantee
    the old `cast()`-narrowed partial fakes gave for free, now that the
    type checker requires each fake to structurally satisfy all three
    `PsServiceClientProtocol` methods rather than just the one under test.
    """

    def list_regulations(self) -> RegulationsResult:
        """Fail: this test's fake does not expect `list_regulations()` to be called."""
        raise AssertionError("list_regulations must not be called in this test")

    def ingest_catalog(self, celex: str, *, run_id: str | None = None) -> IngestionResult:
        """Fail: this test's fake does not expect `ingest_catalog()` to be called."""
        msg = f"ingest_catalog must not be called in this test (celex={celex!r}, run_id={run_id!r})"
        raise AssertionError(msg)

    def ingest_internal(self, fixture_path: str) -> IngestionResult:
        """Fail: this test's fake does not expect `ingest_internal()` to be called."""
        msg = f"ingest_internal must not be called in this test (fixture_path={fixture_path!r})"
        raise AssertionError(msg)

    def poll_ingestion_status(self, run_id: str) -> str | None:
        """Fail: this test's fake does not expect `poll_ingestion_status()` to be called."""
        msg = f"poll_ingestion_status must not be called in this test (run_id={run_id!r})"
        raise AssertionError(msg)


class _FakeRegulationsClient(_UnusedPsServiceClientMethods):
    """Hand-written fake implementing `list_regulations()`'s signature.

    No httpx involved at this layer (PLAN.md §3 Increment 9) -- this is a
    duck-typed stand-in, structurally satisfying `PsServiceClientProtocol`
    (`ps_cli/http_client.py`) with no `cast()` needed (PLAN.md §1 D10).
    """

    def __init__(self, result: RegulationsResult) -> None:
        """Script the RegulationsResult this fake's list_regulations() returns."""
        self._result = result

    def list_regulations(self) -> RegulationsResult:
        """Return the scripted RegulationsResult."""
        return self._result


def test_handle_regulations_list_prints_celex_and_title_per_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each regulation prints as "{celex}  {title}"; no run_id, no header/footer."""
    result = RegulationsResult(
        regulations=[
            RegulationEntry(celex="32016R0679", title="General Data Protection Regulation"),
            RegulationEntry(celex="32019R0881", title="Cybersecurity Act"),
        ],
        run_id="run-abc123",
    )
    fake_client = _FakeRegulationsClient(result)

    handle_regulations_list(fake_client)

    captured = capsys.readouterr()
    assert captured.out == (
        "32016R0679  General Data Protection Regulation\n32019R0881  Cybersecurity Act\n"
    )
    assert captured.err == ""
    assert "run-abc123" not in captured.out


class _FakeIngestClient(_UnusedPsServiceClientMethods):
    """Hand-written fake implementing `ingest_catalog()`'s signature.

    Scripted to either return a fixed `IngestionResult` or raise a fixed
    `PsCliError` (simulating a structured PS Service failure response, e.g.
    Increment 11's 502 case). Records the `celex` it was called with (or
    `None` if never called), for the fast-fail assertion.
    """

    def __init__(
        self, *, result: IngestionResult | None = None, error: PsCliError | None = None
    ) -> None:
        """Script this fake's ingest_catalog() outcome: a result, or an error to raise."""
        self._result = result
        self._error = error
        self.called_with_celex: str | None = None

    def ingest_catalog(self, celex: str, *, run_id: str | None = None) -> IngestionResult:
        """Record `celex`, then return the scripted result or raise the scripted error."""
        del run_id
        self.called_with_celex = celex
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def test_handle_regulations_ingest_prints_run_id_and_stage_summary_on_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A successful ingest prints run_id, regulatory_instrument_id, and each stage's outcome."""
    result = IngestionResult(
        run_id="run-ingest-001",
        regulatory_instrument_id="ri-gdpr",
        source="catalog",
        stages=[
            StageOutcome(stage="parse", status="succeeded", summary={"nodes": 3}),
            StageOutcome(stage="merge", status="succeeded", summary={"edges": 5}),
        ],
    )
    fake = _FakeIngestClient(result=result)

    handle_regulations_ingest("32016R0679", fake)

    captured = capsys.readouterr()
    assert "run_id: run-ingest-001" in captured.out
    assert "regulatory_instrument_id: ri-gdpr" in captured.out
    assert "parse: succeeded" in captured.out
    assert "merge: succeeded" in captured.out
    assert fake.called_with_celex == "32016R0679"


def test_handle_regulations_ingest_propagates_ps_cli_error_from_client_uncaught() -> None:
    """A PsCliError from the client (e.g. a 502) propagates uncaught through the handler.

    Not caught here -- only `ps_cli.cli.run()` catches `PsCliError`, in its
    single central try/except (PLAN.md §1 D5/D9).
    """
    fake = _FakeIngestClient(
        error=PsCliError(
            msg="PS Service reported pipeline_stage_failed: the domain mapper stage failed "
            "(failing stage: domain_mapper)",
            hint="run_id: run-ingest-err",
        )
    )

    with pytest.raises(PsCliError):
        handle_regulations_ingest("32016R0679", fake)


class _FakeProgressIngestClient(_UnusedPsServiceClientMethods):
    """Hand-written fake whose `ingest_catalog()` blocks briefly and whose
    `poll_ingestion_status()` returns a scripted sequence of stages.

    Simulates a real ingest in flight (PLAN.md §3 Increment 15): the main
    thread's `ingest_catalog()` call sleeps for `block_seconds` before
    returning a fixed `IngestionResult`, giving a concurrently-polling
    background thread a real window to observe stage changes.
    """

    def __init__(
        self,
        *,
        result: IngestionResult,
        stages: list[str | None],
        block_seconds: float,
    ) -> None:
        """Script this fake's `ingest_catalog()` delay/result and polled stage sequence."""
        self._result = result
        self._stages = stages
        self._block_seconds = block_seconds
        self._poll_calls = 0

    def ingest_catalog(self, celex: str, *, run_id: str | None = None) -> IngestionResult:
        """Record nothing; block briefly, then return the scripted result."""
        del celex, run_id
        time.sleep(self._block_seconds)
        return self._result

    def poll_ingestion_status(self, run_id: str) -> str | None:
        """Return the next scripted stage, repeating the last one once exhausted."""
        del run_id
        index = min(self._poll_calls, len(self._stages) - 1)
        self._poll_calls += 1
        return self._stages[index]


_PROGRESS_TEST_RESULT = IngestionResult(
    run_id="run-progress-001",
    regulatory_instrument_id="ri-progress",
    source="catalog",
    stages=[StageOutcome(stage="merge", status="succeeded", summary={})],
)


def test_handle_regulations_ingest_prints_stage_changes_to_stderr_while_waiting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stage changes observed while `ingest_catalog()` blocks print to stderr, not stdout.

    The final stdout summary (AC-BI-010) is unchanged by the presence of
    progress output.
    """
    fake = _FakeProgressIngestClient(
        result=_PROGRESS_TEST_RESULT,
        stages=["ingestion", "extraction", "extraction"],
        block_seconds=0.05,
    )

    handle_regulations_ingest("32016R0679", fake, poll_interval_seconds=0.01)

    captured = capsys.readouterr()
    assert "ingestion: running" in captured.err
    assert "extraction: running" in captured.err
    assert captured.out == (
        "run_id: run-progress-001\nregulatory_instrument_id: ri-progress\nmerge: succeeded\n"
    )


def test_handle_regulations_ingest_does_not_repeat_an_unchanged_stage_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stage that stays the same across multiple polls prints only once."""
    fake = _FakeProgressIngestClient(
        result=_PROGRESS_TEST_RESULT,
        stages=["ingestion"],
        block_seconds=0.08,
    )

    handle_regulations_ingest("32016R0679", fake, poll_interval_seconds=0.01)

    captured = capsys.readouterr()
    assert captured.err.count("ingestion: running") == 1


def test_handle_regulations_ingest_stops_polling_after_ingest_catalog_returns() -> None:
    """The poller thread is no longer alive once the handler has returned (no thread leak)."""
    fake = _FakeProgressIngestClient(
        result=_PROGRESS_TEST_RESULT,
        stages=["ingestion", "extraction"],
        block_seconds=0.05,
    )

    handle_regulations_ingest("32016R0679", fake, poll_interval_seconds=0.01)

    poller_threads = [t for t in threading.enumerate() if t.name == "ps-cli-ingest-poller"]
    assert not any(t.is_alive() for t in poller_threads)


def test_handle_regulations_ingest_poll_failures_never_affect_the_final_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`poll_ingestion_status()` always returning `None` never affects the final output."""
    fake = _FakeProgressIngestClient(
        result=_PROGRESS_TEST_RESULT,
        stages=[None, None, None],
        block_seconds=0.05,
    )

    handle_regulations_ingest("32016R0679", fake, poll_interval_seconds=0.01)

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        "run_id: run-progress-001\nregulatory_instrument_id: ri-progress\nmerge: succeeded\n"
    )
