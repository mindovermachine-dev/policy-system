"""Tests for ps_cli.modules.handlers: handle_regulations_list (PLAN.md §3 Increment 9),
handle_regulations_ingest (PLAN.md §3 Increment 12).

`handle_internal_ingest` (PLAN.md §3 Increment 15) is deliberately not
unit-tested at this layer -- per PLAN.md §3 Increment 15 and the batch task
brief, its coverage is exactly two `cli.run()`-level tests in
`ps-cli/tests/test_cli.py`, which prove the full wiring (parser -> dispatch ->
handler -> client) end to end.
"""

from __future__ import annotations

import inspect
import json
import threading
import time
from typing import TYPE_CHECKING

import pytest

from ps_cli.config import CliConfig
from ps_cli.errors import PsCliError
from ps_cli.models import (
    IngestionResult,
    ReadinessResult,
    RegulationEntry,
    RegulationsResult,
    RestorationResult,
    RestorationStageOutcome,
    StageOutcome,
)
from ps_cli.modules.handlers import (
    handle_catalog_list,
    handle_catalog_restore,
    handle_health,
    handle_regulations_ingest,
    handle_regulations_list,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ps_cli.catalog_repo import CuratedArtifact


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

    def check_health(self) -> str:
        """Fail: this test's fake does not expect `check_health()` to be called."""
        raise AssertionError("check_health must not be called in this test")

    def check_readiness(self) -> ReadinessResult:
        """Fail: this test's fake does not expect `check_readiness()` to be called."""
        raise AssertionError("check_readiness must not be called in this test")

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

    def restore_instrument(self, artifact: CuratedArtifact) -> RestorationResult:
        """Fail: this test's fake does not expect `restore_instrument()` to be called."""
        msg = f"restore_instrument must not be called in this test (artifact={artifact!r})"
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


def _write_catalog_fixture(repo_path: Path) -> None:
    (repo_path / "catalog.json").write_text(
        json.dumps(
            [
                {
                    "instrument_id": "CRA-1.0",
                    "title": "Cyber Resilience Act",
                    "source_type": "external",
                    "jurisdiction": "EU",
                },
                {
                    "instrument_id": "ENGPRAC-1.0",
                    "title": "Engineering Practices",
                    "source_type": "internal",
                    "jurisdiction": None,
                },
            ]
        ),
        encoding="utf-8",
    )


def test_handle_catalog_list_prints_instrument_id_title_source_type_and_jurisdiction(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each entry prints as "{id}  {title} ({source_type}, {jurisdiction or 'n/a'})"."""
    _write_catalog_fixture(tmp_path)
    config = CliConfig(service_url="http://127.0.0.1:8000", curated_repo_path=tmp_path)

    handle_catalog_list(config)

    captured = capsys.readouterr()
    assert captured.out == (
        "CRA-1.0  Cyber Resilience Act (external, EU)\n"
        "ENGPRAC-1.0  Engineering Practices (internal, n/a)\n"
    )
    assert captured.err == ""


def test_handle_catalog_list_takes_no_client_parameter() -> None:
    """D13: `catalog list` never constructs a `PsServiceClient` -- proven structurally by
    `handle_catalog_list`'s own signature taking only `config`, unlike every other handler
    in this module (each of which takes a client).
    """
    signature = inspect.signature(handle_catalog_list)

    assert list(signature.parameters) == ["config"]


class _FakeRestoreClient(_UnusedPsServiceClientMethods):
    """Hand-written fake implementing `restore_instrument()`'s signature."""

    def __init__(
        self,
        *,
        result: RestorationResult | None = None,
        error: PsCliError | None = None,
    ) -> None:
        """Script the RestorationResult (or PsCliError) this fake's restore_instrument() returns."""
        self._result = result
        self._error = error
        self.called_with_artifact: CuratedArtifact | None = None

    def restore_instrument(self, artifact: CuratedArtifact) -> RestorationResult:
        """Record `artifact`, then return the scripted result or raise the scripted error."""
        self.called_with_artifact = artifact
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _write_instrument_fixture(repo_path: Path, instrument_id: str) -> None:
    instrument_dir = repo_path / instrument_id
    instrument_dir.mkdir(parents=True)
    manifest = {
        "instrument_id": instrument_id,
        "celex": "32024R2847",
        "title": "Cyber Resilience Act",
        "short_name": "CRA",
        "version": "1.0",
        "source_type": "external",
        "jurisdiction": "EU",
        "schema_version": "1.0.0",
        "exported_at": "2026-09-04T00:00:00Z",
        "baseline_sha256": "a" * 64,
        "native_sha256": "b" * 64,
    }
    (instrument_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (instrument_dir / "baseline.json").write_bytes(b'{"nodes": [], "edges": []}')
    (instrument_dir / "native.json").write_bytes(b'{"nodes": [], "edges": []}')


def test_handle_catalog_restore_prints_instrument_id_and_stage_outcomes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A successful restore prints the instrument id and each completed stage's outcome."""
    _write_instrument_fixture(tmp_path, "CRA-1.0")
    result = RestorationResult(
        instrument_id="CRA-1.0",
        stages=[
            RestorationStageOutcome(stage="verified", status="succeeded"),
            RestorationStageOutcome(stage="staged", status="succeeded"),
        ],
    )
    fake = _FakeRestoreClient(result=result)

    handle_catalog_restore("CRA-1.0", fake, curated_repo_path=tmp_path)

    captured = capsys.readouterr()
    assert "instrument_id: CRA-1.0" in captured.out
    assert "verified: succeeded" in captured.out
    assert "staged: succeeded" in captured.out
    assert fake.called_with_artifact is not None
    assert fake.called_with_artifact.manifest.instrument_id == "CRA-1.0"
    assert fake.called_with_artifact.manifest.short_name == "CRA"


def test_handle_catalog_restore_propagates_ps_cli_error_from_client_uncaught(
    tmp_path: Path,
) -> None:
    """A PsCliError from the client (e.g. a 422 checksum rejection) propagates uncaught.

    Not caught here -- only `ps_cli.cli.run()` catches `PsCliError`, in its single
    central try/except (PLAN.md §1 D5/D9).
    """
    _write_instrument_fixture(tmp_path, "CRA-1.0")
    fake = _FakeRestoreClient(
        error=PsCliError(msg="PS Service reported restore_artifact_rejected: bad checksum")
    )

    with pytest.raises(PsCliError):
        handle_catalog_restore("CRA-1.0", fake, curated_repo_path=tmp_path)


def test_handle_catalog_restore_propagates_ps_cli_error_when_local_artifact_missing(
    tmp_path: Path,
) -> None:
    """A missing local instrument directory raises PsCliError before the client is ever called."""
    fake = _FakeRestoreClient()

    with pytest.raises(PsCliError):
        handle_catalog_restore("MISSING-1.0", fake, curated_repo_path=tmp_path)

    assert fake.called_with_artifact is None


class _FakeHealthClient(_UnusedPsServiceClientMethods):
    """Hand-written fake implementing `check_health()`/`check_readiness()`'s signatures."""

    def __init__(self, *, health_status: str, readiness: ReadinessResult) -> None:
        """Script this fake's `check_health()`/`check_readiness()` return values."""
        self._health_status = health_status
        self._readiness = readiness

    def check_health(self) -> str:
        """Return the scripted health status."""
        return self._health_status

    def check_readiness(self) -> ReadinessResult:
        """Return the scripted readiness result."""
        return self._readiness


def test_handle_health_prints_reachable_alive_ready_on_happy_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A fully healthy, fully ready target prints exactly the three summary lines (D10/D11)."""
    fake = _FakeHealthClient(
        health_status="alive",
        readiness=ReadinessResult(status="ready", unhealthy_dependencies=[]),
    )

    handle_health(fake)

    captured = capsys.readouterr()
    assert captured.out == "reachable: yes\nhealth: alive\nready: ready\n"
    assert captured.err == ""


def test_handle_health_raises_ps_cli_error_naming_unhealthy_dependencies_when_not_ready() -> None:
    """A not-ready target with a named unhealthy dependency raises PsCliError with that name
    in the hint (AC-BI-001 handler half, AC-BI-007).
    """
    fake = _FakeHealthClient(
        health_status="alive",
        readiness=ReadinessResult(status="not_ready", unhealthy_dependencies=["falkordb"]),
    )

    with pytest.raises(PsCliError) as excinfo:
        handle_health(fake)

    assert "not ready" in excinfo.value.msg
    assert excinfo.value.hint is not None
    assert "falkordb" in excinfo.value.hint


def test_handle_health_raises_ps_cli_error_with_no_hint_when_no_dependency_named() -> None:
    """A not-ready target with no unhealthy dependency named (§0.3's line-970 nuance) still
    raises, but with no hint -- proving the hint is genuinely conditional, not always-present.
    """
    fake = _FakeHealthClient(
        health_status="alive",
        readiness=ReadinessResult(status="not_ready", unhealthy_dependencies=[]),
    )

    with pytest.raises(PsCliError) as excinfo:
        handle_health(fake)

    assert excinfo.value.hint is None


def test_handle_health_not_ready_message_never_says_could_not_reach() -> None:
    """The not-ready error's rendered text is textually distinct from the unreachable-target
    error's wording (AC-BI-008), proven by an executed assertion, not just inspection.
    """
    fake = _FakeHealthClient(
        health_status="alive",
        readiness=ReadinessResult(status="not_ready", unhealthy_dependencies=["falkordb"]),
    )

    with pytest.raises(PsCliError) as excinfo:
        handle_health(fake)

    assert "Could not reach" not in str(excinfo.value)
