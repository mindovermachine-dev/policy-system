"""Tests for ps_cli.cli: run() (which wires ps_cli.modules.parser.build_parser() and
ps_cli.modules.handlers.DISPATCH together), and __main__.py's AST shape.

PLAN.md §3 Increments 10, 12, 13.
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest

from ps_cli.cli import run
from ps_cli.errors import PsCliError
from ps_cli.models import IngestionResult, RegulationsResult

_MAIN_MODULE_PATH = Path(__file__).resolve().parent.parent / "src" / "ps_cli" / "__main__.py"


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


class _FakeSuccessClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose list_regulations() succeeds."""

    def list_regulations(self) -> RegulationsResult:
        """Return an empty-but-valid RegulationsResult."""
        return RegulationsResult(regulations=[], run_id="run-fake-success")


class _FakeFailingClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose list_regulations() always raises."""

    def list_regulations(self) -> RegulationsResult:
        """Raise a PsCliError, simulating a PS Service failure response."""
        raise PsCliError(
            msg="PS Service reported catalog_identifier_not_found: CELEX not found",
            hint="check the CELEX identifier and try again",
        )


def test_run_regulations_list_returns_zero_on_success() -> None:
    """`run(["regulations", "list"], client=<succeeding fake>)` returns 0."""
    fake_client = _FakeSuccessClient()

    exit_code = run(["regulations", "list"], client=fake_client)

    assert exit_code == 0


def test_run_formats_ps_cli_error_to_stderr_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A PsCliError from the handler: exit 1, msg+hint on stderr, no traceback, no stdout."""
    fake_client = _FakeFailingClient()

    exit_code = run(["regulations", "list"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "PS Service reported catalog_identifier_not_found: CELEX not found" in captured.err
    assert "check the CELEX identifier and try again" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_run_formats_ps_cli_error_with_failure_site_when_verbose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`-v`/`--verbose` adds a `🔦 @ file:line` diagnostic line after the formatted error."""
    fake_client = _FakeFailingClient()

    exit_code = run(["-v", "regulations", "list"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "🔦 @ " in captured.err
    assert ".py:" in captured.err


def test_run_omits_failure_site_when_not_verbose(capsys: pytest.CaptureFixture[str]) -> None:
    """Without `-v`, no `🔦` diagnostic line is printed -- just the formatted error."""
    fake_client = _FakeFailingClient()

    run(["regulations", "list"], client=fake_client)

    assert "🔦" not in capsys.readouterr().err


def test_run_with_no_command_prints_help_and_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bare `ps-cli` (no subcommand, no `--version`) prints help and returns 0 (mirrors gh-tt)."""
    exit_code = run([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "usage: ps-cli" in captured.out


def test_run_with_version_flag_prints_version_and_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`ps-cli --version` prints the installed version and returns 0 -- no subcommand needed."""
    exit_code = run(["--version"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip() != ""


def test_main_module_only_imports_and_conditionally_calls_main() -> None:
    """AC-BI-005 literal proof: __main__.py's AST is exactly an import + a conditional call.

    Parses `ps_cli/__main__.py`'s source and asserts its module body contains
    exactly two statements: an `ImportFrom` (`from ps_cli.cli import main`) and
    an `If` whose only body statement is a bare call to `main()` -- nothing
    else lives in this file (L2's "no logic lives in __main__.py itself").
    """
    source = _MAIN_MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert len(tree.body) == 2
    import_node, if_node = tree.body

    assert isinstance(import_node, ast.ImportFrom)
    assert import_node.module == "ps_cli.cli"
    assert [alias.name for alias in import_node.names] == ["main"]

    assert isinstance(if_node, ast.If)
    assert if_node.orelse == []
    assert len(if_node.body) == 1
    expr_node = if_node.body[0]
    assert isinstance(expr_node, ast.Expr)
    assert isinstance(expr_node.value, ast.Call)
    assert isinstance(expr_node.value.func, ast.Name)
    assert expr_node.value.func.id == "main"
    assert expr_node.value.args == []
    assert expr_node.value.keywords == []


class _FakeIngestSuccessClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose ingest_catalog() succeeds."""

    def ingest_catalog(self, celex: str, *, run_id: str | None = None) -> IngestionResult:
        """Return a fixed IngestionResult, ignoring `celex`/`run_id`."""
        del celex, run_id
        return IngestionResult(
            run_id="run-ingest-cli",
            regulatory_instrument_id="ri-cli",
            source="catalog",
            stages=[],
        )


class _FakeIngestFailingClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose ingest_catalog() always raises."""

    def ingest_catalog(self, celex: str, *, run_id: str | None = None) -> IngestionResult:
        """Raise a PsCliError, simulating a 502 pipeline_stage_failed response."""
        del celex, run_id
        raise PsCliError(
            msg="PS Service reported pipeline_stage_failed: the domain mapper stage failed "
            "(failing stage: domain_mapper)",
            hint="run_id: run-ingest-err",
        )


class _UncallableIngestClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose ingest_catalog() must never be called."""

    def ingest_catalog(self, celex: str, *, run_id: str | None = None) -> IngestionResult:
        """Fail the test if reached -- proves the CLI validated `celex` before calling out."""
        del run_id
        msg = f"ingest_catalog must not be called for a malformed celex, got {celex!r}"
        raise AssertionError(msg)


def test_run_regulations_ingest_returns_zero_on_success() -> None:
    """`run(["regulations", "ingest", <celex>], client=<succeeding fake>)` returns 0."""
    fake_client = _FakeIngestSuccessClient()

    exit_code = run(["regulations", "ingest", "32016R0679"], client=fake_client)

    assert exit_code == 0


def test_run_regulations_ingest_prints_run_id_on_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-010: the run id is printed to stdout on a successful ingest."""
    fake_client = _FakeIngestSuccessClient()

    run(["regulations", "ingest", "32016R0679"], client=fake_client)

    captured = capsys.readouterr()
    assert "run_id: run-ingest-cli" in captured.out


def test_run_regulations_ingest_malformed_celex_exits_two_without_calling_client(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A malformed CELEX is rejected by argparse's `type=` callback (PLAN.md §1 D10).

    This now happens during `parser.parse_args()`, before `run()`'s own
    `try/except PsCliError` is even entered -- argparse's own machinery exits
    2 via `SystemExit`, not `run()`'s return value (exit 1 is reserved for
    `PsCliError`s raised after a successful parse). The client is never
    constructed far enough to be called either way.
    """
    fake_client = _UncallableIngestClient()

    with pytest.raises(SystemExit) as excinfo:
        run(["regulations", "ingest", "not-a-celex"], client=fake_client)

    assert excinfo.value.code == 2
    assert "not a 10-character CELEX identifier" in capsys.readouterr().err


class _FakeIngestRecordingClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in that records the `celex` it was called with."""

    def __init__(self) -> None:
        """Initialize with no recorded call yet."""
        self.called_with_celex: str | None = None

    def ingest_catalog(self, celex: str, *, run_id: str | None = None) -> IngestionResult:
        """Record `celex`, then return a fixed IngestionResult."""
        del run_id
        self.called_with_celex = celex
        return IngestionResult(
            run_id="run-ingest-cli",
            regulatory_instrument_id="ri-cli",
            source="catalog",
            stages=[],
        )


def test_run_regulations_ingest_trims_whitespace_padded_celex_before_calling_client() -> None:
    """AC-BI-001: a whitespace-padded CELEX is trimmed before it reaches the client."""
    fake_client = _FakeIngestRecordingClient()

    run(["regulations", "ingest", "  32016R0679  "], client=fake_client)

    assert fake_client.called_with_celex == "32016R0679"


def test_run_regulations_ingest_propagates_client_ps_cli_error_as_exit_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A PsCliError raised by ingest_catalog() (e.g. a 502) is caught centrally by run()."""
    fake_client = _FakeIngestFailingClient()

    exit_code = run(["regulations", "ingest", "32016R0679"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "pipeline_stage_failed" in captured.err
    assert "domain_mapper" in captured.err
    assert "Traceback" not in captured.err


def test_run_with_unreachable_real_service_returns_one_without_crashing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-007's real-network proof (PLAN.md §3 Increment 13).

    `run()` with `client=None` builds a real `PsServiceClient` from
    `PS_CLI_SERVICE_URL`, pointed at a definitely-closed local port (bind a
    socket, close it, reuse the freed port number -- a small accepted TOCTOU
    flake risk per PLAN.md, not engineered away). Asserts an actionable
    message on stderr, exit code 1, and -- by simply not wrapping the call in
    `pytest.raises` -- that no exception escapes `run()`.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setenv("PS_CLI_SERVICE_URL", f"http://127.0.0.1:{port}")

    exit_code = run(["regulations", "list"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Could not reach PS Service at" in captured.err
    assert "Traceback" not in captured.err


# The exact, current string from ps_service/api/routes.py's
# _INTERNAL_NOT_IMPLEMENTED_MESSAGE constant -- confirmed by reading that file
# (read-only reference; ps-cli never imports ps_service). This is what a real,
# unmodified ps-service returns today for `internal ingest`, via a 501
# response `PsServiceClient.ingest_internal()` maps to a `PsCliError` carrying
# this exact message (per Increment 14's `TestIngestInternal`).
_INTERNAL_NOT_IMPLEMENTED_MESSAGE = (
    "Internal-document ingestion is not implemented in this walking-skeleton "
    "release; it is tracked in issue #54 (mindovermachine-dev/policy-system)."
)


class _FakeInternalIngestSuccessClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose ingest_internal() succeeds."""

    def ingest_internal(self, fixture_path: str) -> IngestionResult:
        """Return a fixed IngestionResult, ignoring `fixture_path`."""
        del fixture_path
        return IngestionResult(
            run_id="run-internal-cli",
            regulatory_instrument_id="ri-internal-cli",
            source="internal",
            stages=[],
        )


class _FakeInternalIngest501Client(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose ingest_internal() raises the real 501.

    Mirrors exactly what `PsServiceClient.ingest_internal()` raises for the
    real, unmodified ps-service's current `internal_ingestion_not_implemented`
    501 response (Increment 14's `TestIngestInternal`), so this test proves
    `cli.run()`'s handling of that failure without a real network call.
    """

    def ingest_internal(self, fixture_path: str) -> IngestionResult:
        """Raise the PsCliError PsServiceClient.ingest_internal() raises for the real 501."""
        del fixture_path
        raise PsCliError(
            msg=f"PS Service reported internal_ingestion_not_implemented: "
            f"{_INTERNAL_NOT_IMPLEMENTED_MESSAGE}",
            hint="run_id: run-internal-501",
        )


def test_internal_ingest_prints_run_id_on_mocked_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Covers AC-BI-003's CLI plumbing only, per orchestrator decision 1 (2026-08-30).

    PS Service's real internal pipeline is issue #54, not started; this test
    proves ps-cli's command/argument/REST wiring against a mocked success
    response -- it is NOT proof the real service can ingest an internal
    fixture, and AC-BI-003 must not be marked done on the strength of this
    test alone.
    """
    fake_client = _FakeInternalIngestSuccessClient()

    exit_code = run(["internal", "ingest", "seeds/internal-sop.json"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "run_id: run-internal-cli" in captured.out


def test_internal_ingest_surfaces_real_service_501_as_clean_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What running this command against the real, unmodified ps-service produces today.

    Expected and correct per orchestrator decision 1 -- PS Service's
    `POST /ingestions` with `source: "internal"` currently 501s
    (`internal_ingestion_not_implemented`) until issue #54's backend lands.
    Asserts AC-BI-008's shape: exit 1, the `internal_ingestion_not_implemented`
    message surfaced in stderr, no traceback substring present.
    """
    fake_client = _FakeInternalIngest501Client()

    exit_code = run(["internal", "ingest", "seeds/internal-sop.json"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "internal_ingestion_not_implemented" in captured.err
    assert _INTERNAL_NOT_IMPLEMENTED_MESSAGE in captured.err
    assert "Traceback" not in captured.err
