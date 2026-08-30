"""Tests for ps_cli.modules.handlers: handle_regulations_list (PLAN.md §3 Increment 9),
handle_regulations_ingest (PLAN.md §3 Increment 12).

`handle_internal_ingest` (PLAN.md §3 Increment 15) is deliberately not
unit-tested at this layer -- per PLAN.md §3 Increment 15 and the batch task
brief, its coverage is exactly two `cli.run()`-level tests in
`ps-cli/tests/test_cli.py`, which prove the full wiring (parser -> dispatch ->
handler -> client) end to end.
"""

from __future__ import annotations

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

    def ingest_catalog(self, celex: str) -> IngestionResult:
        """Fail: this test's fake does not expect `ingest_catalog()` to be called."""
        msg = f"ingest_catalog must not be called in this test (celex={celex!r})"
        raise AssertionError(msg)

    def ingest_internal(self, fixture_path: str) -> IngestionResult:
        """Fail: this test's fake does not expect `ingest_internal()` to be called."""
        msg = f"ingest_internal must not be called in this test (fixture_path={fixture_path!r})"
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

    def ingest_catalog(self, celex: str) -> IngestionResult:
        """Record `celex`, then return the scripted result or raise the scripted error."""
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
