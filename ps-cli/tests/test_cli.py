"""Tests for ps_cli.cli: run() (which wires ps_cli.modules.parser.build_parser() and
ps_cli.modules.handlers.DISPATCH together), and __main__.py's AST shape.

PLAN.md §3 Increments 10, 12, 13.
"""

from __future__ import annotations

import ast
import base64
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ps_cli import cli
from ps_cli.cli import run
from ps_cli.config import load_config
from ps_cli.credentials import TokenBundle, build_credential_store
from ps_cli.device_flow import poll_for_token, request_device_authorization
from ps_cli.errors import PsCliError
from ps_cli.models import (
    ChangeCheckResult,
    ExportManifest,
    ExportResult,
    ExportStageOutcome,
    IngestionResult,
    InstrumentCheckOutcome,
    PendingReviewsResult,
    ReadinessResult,
    ResolveReviewResult,
    RestorationResult,
    RestorationStageOutcome,
)
from ps_cli.modules.parser import build_parser
from ps_cli.oidc_discovery import ResolvedAuthParameters
from ps_cli.targets import load_targets
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from conftest import AlwaysRaisingPersistenceBackend, InMemoryPersistenceBackend

    from ps_cli.catalog_repo import CuratedArtifact
    from ps_test_support.mock_oidc_provider import MockOidcProvider

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

    def check_health(self) -> str:
        """Fail: this test's fake does not expect `check_health()` to be called."""
        raise AssertionError("check_health must not be called in this test")

    def get_service_version(self) -> str:
        """Fail: this test's fake does not expect `get_service_version()` to be called."""
        raise AssertionError("get_service_version must not be called in this test")

    def check_readiness(self) -> ReadinessResult:
        """Fail: this test's fake does not expect `check_readiness()` to be called."""
        raise AssertionError("check_readiness must not be called in this test")

    def ingest_catalog(self, celex: str, *, run_id: str | None = None) -> IngestionResult:
        """Fail: this test's fake does not expect `ingest_catalog()` to be called."""
        msg = f"ingest_catalog must not be called in this test (celex={celex!r}, run_id={run_id!r})"
        raise AssertionError(msg)

    def ingest_internal(self, content: dict[str, object]) -> IngestionResult:
        """Fail: this test's fake does not expect `ingest_internal()` to be called."""
        msg = f"ingest_internal must not be called in this test (content={content!r})"
        raise AssertionError(msg)

    def poll_ingestion_status(self, run_id: str) -> str | None:
        """Fail: this test's fake does not expect `poll_ingestion_status()` to be called."""
        msg = f"poll_ingestion_status must not be called in this test (run_id={run_id!r})"
        raise AssertionError(msg)

    def restore_instrument(self, artifact: CuratedArtifact) -> RestorationResult:
        """Fail: this test's fake does not expect `restore_instrument()` to be called."""
        msg = f"restore_instrument must not be called in this test (artifact={artifact!r})"
        raise AssertionError(msg)

    def export_instrument(self, instrument_id: str) -> ExportResult:
        """Fail: this test's fake does not expect `export_instrument()` to be called."""
        msg = f"export_instrument must not be called in this test (instrument_id={instrument_id!r})"
        raise AssertionError(msg)

    def run_change_check(self) -> ChangeCheckResult:
        """Fail: this test's fake does not expect `run_change_check()` to be called."""
        raise AssertionError("run_change_check must not be called in this test")

    def list_pending_reviews(self) -> PendingReviewsResult:
        """Fail: this test's fake does not expect `list_pending_reviews()` to be called."""
        raise AssertionError("list_pending_reviews must not be called in this test")

    def resolve_review(self, review_id: str, decision: str) -> ResolveReviewResult:
        """Fail: this test's fake does not expect `resolve_review()` to be called."""
        msg = (
            f"resolve_review must not be called in this test "
            f"(review_id={review_id!r}, decision={decision!r})"
        )
        raise AssertionError(msg)


def _fake_installed_version(name: str) -> str:
    """Return a fixed "1.4.0" regardless of `name` -- a typed stand-in for `installed_
    version` (mirrors `ps-service/tests/test_main.py`'s own `fake_installed_version`
    pattern; a bare `lambda` here fails `basedpyright` strict's unknown-parameter-type
    checks, since `importlib.metadata.version`'s parameter type cannot be inferred from
    an unannotated lambda).
    """
    del name
    return "1.4.0"


class _FakeVersionClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in with a scripted get_service_version()."""

    def __init__(self, service_version: str) -> None:
        """Store the version string this fake's get_service_version() returns."""
        self._service_version = service_version

    def get_service_version(self) -> str:
        """Return the scripted service version string."""
        return self._service_version


class _FakeVersionErrorClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose get_service_version() always raises."""

    def __init__(self, error: PsCliError) -> None:
        """Store the PsCliError this fake's get_service_version() raises."""
        self._error = error

    def get_service_version(self) -> str:
        """Raise the scripted PsCliError."""
        raise self._error


class _FakeFailingClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose check_health() always raises."""

    def check_health(self) -> str:
        """Raise a PsCliError, simulating a PS Service failure response."""
        raise PsCliError(
            msg="PS Service reported catalog_identifier_not_found: CELEX not found",
            hint="check the CELEX identifier and try again",
        )


class _FakeKeyboardInterruptClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose check_health() raises KeyboardInterrupt.

    Simulates an operator pressing Ctrl-C mid-call (issue #122) -- `check_health()`
    stands in for any blocking call; `run()`'s own catch site is command-agnostic
    (AC-BI-007), so this single fake proves the general boundary, not anything
    specific to `get health`.
    """

    def check_health(self) -> str:
        """Raise KeyboardInterrupt, simulating Ctrl-C during a blocking call."""
        raise KeyboardInterrupt


class _FakeUnexpectedErrorClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose check_health() raises an unhandled bug."""

    def check_health(self) -> str:
        """Raise a plain RuntimeError, simulating a genuine, un-classified ps-cli bug."""
        raise RuntimeError("boom")


class _FakeNearMissesSuccessClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose list_pending_reviews() succeeds."""

    def list_pending_reviews(self) -> PendingReviewsResult:
        """Return an empty-but-valid PendingReviewsResult."""
        return PendingReviewsResult(reviews=[])


class _FakeNearMissesFailingClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose list_pending_reviews() always raises."""

    def list_pending_reviews(self) -> PendingReviewsResult:
        """Raise a PsCliError, simulating a PS Service failure response."""
        raise PsCliError(msg="Could not reach PS Service at http://127.0.0.1:8000.")


def test_run_near_misses_list_returns_zero_on_success() -> None:
    """`run(["near-misses", "list"], client=<succeeding fake>)` returns 0 (issue #35, AC-BI-003)."""
    fake_client = _FakeNearMissesSuccessClient()

    exit_code = run(["near-misses", "list"], client=fake_client)

    assert exit_code == 0


def test_run_near_misses_list_returns_one_on_ps_cli_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `PsCliError` from the client surfaces as exit code 1, per `run()`'s one catch site."""
    fake_client = _FakeNearMissesFailingClient()

    exit_code = run(["near-misses", "list"], client=fake_client)

    assert exit_code == 1
    assert "Could not reach PS Service" in capsys.readouterr().err


class _FakeNearMissesResolveSuccessClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose resolve_review() succeeds."""

    def resolve_review(self, review_id: str, decision: str) -> ResolveReviewResult:
        """Return a scripted ResolveReviewResult echoing the given id/decision."""
        return ResolveReviewResult(
            review_id=review_id, decision=decision, winner_id=None, loser_id=None
        )


class _FakeNearMissesResolveMergeSuccessClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose resolve_review() succeeds with a merge."""

    def resolve_review(self, review_id: str, decision: str) -> ResolveReviewResult:
        """Return a scripted ResolveReviewResult with winner/loser ids populated."""
        return ResolveReviewResult(
            review_id=review_id,
            decision=decision,
            winner_id="capability_winner",
            loser_id="capability_loser",
        )


class _FakeNearMissesResolveFailingClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose resolve_review() always raises (AC-BI-008)."""

    def resolve_review(self, review_id: str, decision: str) -> ResolveReviewResult:
        """Raise a PsCliError, simulating a not-found PS Service response."""
        del review_id, decision
        raise PsCliError(
            msg="PS Service reported pending_review_not_found: no unresolved PendingReview"
        )


def test_run_near_misses_resolve_keep_separate_returns_zero_on_success() -> None:
    """`run(["near-misses", "resolve", ..., "--decision=keep-separate"])` returns 0 (AC-BI-004)."""
    fake_client = _FakeNearMissesResolveSuccessClient()

    exit_code = run(
        ["near-misses", "resolve", "review_aaa", "--decision", "keep-separate"],
        client=fake_client,
    )

    assert exit_code == 0


def test_run_near_misses_resolve_returns_one_on_ps_cli_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-008: a not-found review id's PsCliError surfaces as exit code 1."""
    fake_client = _FakeNearMissesResolveFailingClient()

    exit_code = run(
        ["near-misses", "resolve", "review_missing", "--decision", "keep-separate"],
        client=fake_client,
    )

    assert exit_code == 1
    assert "pending_review_not_found" in capsys.readouterr().err


def test_run_near_misses_resolve_merge_returns_zero_on_success() -> None:
    """`run(["near-misses", "resolve", ..., "--decision=merge"])` returns 0 (AC-BI-005/006/007)."""
    fake_client = _FakeNearMissesResolveMergeSuccessClient()

    exit_code = run(
        ["near-misses", "resolve", "review_aaa", "--decision", "merge"],
        client=fake_client,
    )

    assert exit_code == 0


def test_run_near_misses_resolve_rejects_unknown_decision_at_parse_time() -> None:
    """The parser's `--decision` `choices` rejects any value outside keep-separate/merge.

    Argparse rejects an unknown `choices` value before any dispatch/client
    call happens (exit code 2, a usage error) -- `_UnusedPsServiceClientMethods`'s
    bare fake would raise `AssertionError` on any client call, proving none
    was made.
    """
    uncallable_client = _UnusedPsServiceClientMethods()

    with pytest.raises(SystemExit) as excinfo:
        run(
            ["near-misses", "resolve", "review_aaa", "--decision", "not-a-decision"],
            client=uncallable_client,
        )

    assert excinfo.value.code == 2


def test_run_formats_ps_cli_error_to_stderr_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A PsCliError from the handler: exit 1, msg+hint on stderr, no traceback, no stdout."""
    fake_client = _FakeFailingClient()

    exit_code = run(["get", "health"], client=fake_client)

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

    exit_code = run(["-v", "get", "health"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "🔦 @ " in captured.err
    assert ".py:" in captured.err


def test_run_omits_failure_site_when_not_verbose(capsys: pytest.CaptureFixture[str]) -> None:
    """Without `-v`, no `🔦` diagnostic line is printed -- just the formatted error."""
    fake_client = _FakeFailingClient()

    run(["get", "health"], client=fake_client)

    assert "🔦" not in capsys.readouterr().err


def test_run_returns_130_and_prints_cancelled_on_keyboard_interrupt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-001/007/010: Ctrl-C during any blocking command exits 130, cancel message
    to stderr only, no traceback -- not specific to `auth login` (`get health` here).
    """
    fake_client = _FakeKeyboardInterruptClient()

    exit_code = run(["get", "health"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 130
    assert captured.err == "cancelled\n"
    assert captured.out == ""
    assert "Traceback" not in captured.err


def test_run_keyboard_interrupt_message_is_not_formatted_as_ps_cli_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-004: the cancel message is plain text, never `PsCliError`'s `❌`/`💡` shape."""
    fake_client = _FakeKeyboardInterruptClient()

    run(["get", "health"], client=fake_client)

    captured = capsys.readouterr()
    assert "❌" not in captured.err
    assert "💡" not in captured.err


def test_run_returns_two_and_prints_one_line_on_unexpected_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-002/009/010: an unhandled bug exits 2, one `{type}: {message}` line to
    stderr only, no traceback -- unless `-v` was given (separate test below).
    """
    fake_client = _FakeUnexpectedErrorClient()

    exit_code = run(["get", "health"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.err == "RuntimeError: boom\n"
    assert captured.out == ""
    assert "Traceback" not in captured.err


def test_run_prints_full_traceback_for_unexpected_exception_when_verbose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-005: `-v` on an unhandled bug prints the full traceback, not just a
    single-frame pointer (unlike `PsCliError`'s own `-v` behavior) -- still exits 2.
    """
    fake_client = _FakeUnexpectedErrorClient()

    exit_code = run(["-v", "get", "health"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "RuntimeError: boom" in captured.err
    assert "Traceback (most recent call last)" in captured.err


def test_run_with_no_command_prints_help_and_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bare `ps-cli` (no subcommand, no `--version`) prints help and returns 0 (mirrors gh-tt)."""
    exit_code = run([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "usage: ps-cli" in captured.out


def test_run_version_prints_two_lines_and_returns_zero_when_versions_match(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ps-cli --version` prints client then service version lines and returns 0.

    Supersedes the old `test_run_with_version_flag_prints_version_and_returns_zero`,
    which called `run(["--version"])` with `client=None` -- now that `--version`
    resolves a client and calls `get_service_version()` (AC-BI-005/007), that would
    silently perform a real network call on every test run instead of testing what
    its name promised (PLAN.md §5 Slice 5, flagged as a decided fold-in, not a silent
    drop, in §7 Risk 6).
    """
    monkeypatch.setattr(cli, "installed_version", _fake_installed_version)
    fake_client = _FakeVersionClient("1.4.0")

    exit_code = run(["--version"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "PS-CLI Client Version: 1.4.0\nPS-Service Version: 1.4.0\n"
    assert captured.err == ""


def test_run_version_reports_unavailable_with_error_msg_when_service_client_raises(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-008: a `PsCliError` from `get_service_version()` reports "unavailable (.msg)".

    Uses `.msg` verbatim, never `str(error)` (which would add the `❌`/`💡` decoration --
    `errors.py`'s `PsCliError.__str__`) -- and prints to stdout, not stderr, per D7/CHANGES.md
    X-06 (user-confirmed, closed).
    """
    fake_client = _FakeVersionErrorClient(
        PsCliError(
            msg="Could not reach PS Service at http://x.",
            hint="check PS_CLI_SERVICE_URL / ps-cli.toml, and that ps-service is running",
        )
    )

    exit_code = run(["--version"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    lines = captured.out.splitlines()
    assert lines[1] == "PS-Service Version: unavailable (Could not reach PS Service at http://x.)"
    assert captured.err == ""


def test_run_version_reports_unavailable_when_config_resolution_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-BI-008: `_resolve_client`'s own `load_config()` failure is caught the same way.

    Reuses this file's own broken-`targets.toml` fixture (`current_context` naming a
    context absent from `[contexts]`, e.g. `test_run_config_set_context_never_constructs_
    ps_service_client`) -- `client=None` forces a real `_resolve_client(args, None)` call.
    """
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    (tmp_path / "targets.toml").write_text(
        'current_context = "missing"\n\n[contexts.dev]\nurl = "http://127.0.0.1:8000"\n'
    )

    exit_code = run(["--version"], client=None)

    assert exit_code == 0


def test_run_version_with_unreachable_real_service_returns_zero_without_crashing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-008's real-network proof, mirroring `test_run_with_unreachable_real_service_
    returns_one_without_crashing`'s exact bind-then-close-socket pattern -- but `--version`
    returns 0, not 1, and the failure is reported on stdout, not stderr.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setenv("PS_CLI_SERVICE_URL", f"http://127.0.0.1:{port}")

    exit_code = run(["--version"], client=None)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Could not reach PS Service at" in captured.out.splitlines()[1]
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err
    assert captured.err == ""


def test_run_version_prints_client_version_line_even_when_service_is_unavailable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The client-version line is unconditional, printed before the service lookup is even
    attempted -- so it still appears as the first stdout line when the service is unreachable.
    """
    fake_client = _FakeVersionErrorClient(PsCliError(msg="Could not reach PS Service at http://x."))

    exit_code = run(["--version"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    lines = captured.out.splitlines()
    assert lines[0].startswith("PS-CLI Client Version: ")


def test_run_version_warns_on_stderr_when_client_and_service_versions_differ(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-009: a version mismatch prints exactly one `warning: ...` line to stderr.

    Only the success path can trigger this (not the "unavailable" path, D6) -- stdout
    still carries its normal two lines, and exit code stays 0.
    """
    monkeypatch.setattr(cli, "installed_version", _fake_installed_version)
    fake_client = _FakeVersionClient("2.0.0")

    exit_code = run(["--version"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "PS-CLI Client Version: 1.4.0\nPS-Service Version: 2.0.0\n"
    assert captured.err == (
        "warning: ps-cli client version (1.4.0) does not match ps-service version (2.0.0)\n"
    )


def test_run_version_emits_no_warning_when_versions_match(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Negative case for AC-BI-009: matching versions print nothing to stderr."""
    monkeypatch.setattr(cli, "installed_version", _fake_installed_version)
    fake_client = _FakeVersionClient("1.4.0")

    run(["--version"], client=fake_client)

    assert capsys.readouterr().err == ""


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

    def check_readiness(self) -> ReadinessResult:
        """Report a fully-healthy target -- the pre-flight check must let this through."""
        return ReadinessResult(status="ready", unhealthy_dependencies=[])

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

    def check_readiness(self) -> ReadinessResult:
        """Report a fully-healthy target -- the pre-flight check must let this through."""
        return ReadinessResult(status="ready", unhealthy_dependencies=[])

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


def test_run_ingest_regulation_returns_zero_on_success() -> None:
    """`run(["ingest", "regulation", <celex>], client=<succeeding fake>)` returns 0."""
    fake_client = _FakeIngestSuccessClient()

    exit_code = run(["ingest", "regulation", "32016R0679"], client=fake_client)

    assert exit_code == 0


def test_run_ingest_regulation_prints_run_id_on_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-010: the run id is printed to stdout on a successful ingest."""
    fake_client = _FakeIngestSuccessClient()

    run(["ingest", "regulation", "32016R0679"], client=fake_client)

    captured = capsys.readouterr()
    assert "run_id: run-ingest-cli" in captured.out


def test_run_ingest_regulation_malformed_celex_exits_two_without_calling_client(
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
        run(["ingest", "regulation", "not-a-celex"], client=fake_client)

    assert excinfo.value.code == 2
    assert "not a 10-character CELEX identifier" in capsys.readouterr().err


class _FakeIngestRecordingClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in that records the `celex` it was called with."""

    def __init__(self) -> None:
        """Initialize with no recorded call yet."""
        self.called_with_celex: str | None = None

    def check_readiness(self) -> ReadinessResult:
        """Report a fully-healthy target -- the pre-flight check must let this through."""
        return ReadinessResult(status="ready", unhealthy_dependencies=[])

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


def test_run_ingest_regulation_trims_whitespace_padded_celex_before_calling_client() -> None:
    """AC-BI-001: a whitespace-padded CELEX is trimmed before it reaches the client."""
    fake_client = _FakeIngestRecordingClient()

    run(["ingest", "regulation", "  32016R0679  "], client=fake_client)

    assert fake_client.called_with_celex == "32016R0679"


def test_run_ingest_regulation_propagates_client_ps_cli_error_as_exit_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A PsCliError raised by ingest_catalog() (e.g. a 502) is caught centrally by run()."""
    fake_client = _FakeIngestFailingClient()

    exit_code = run(["ingest", "regulation", "32016R0679"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "pipeline_stage_failed" in captured.err
    assert "domain_mapper" in captured.err
    assert "Traceback" not in captured.err


class _FakeReadinessGatedIngestClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in scripting `check_readiness()`'s outcome.

    Used to prove the pre-flight check (AC-BI-009..013): either a scripted
    `ReadinessResult` is returned, or a scripted `PsCliError` is raised (simulating PS
    Service itself being unreachable, `http_client.py:105-110`'s shape). By default
    `ingest_catalog()` raises `AssertionError` if called -- proving the pre-flight check
    ran first and blocked the command; pass `allow_ingest=True` for the one test where
    the pre-flight check must let the command proceed to a normal, successful ingest.
    """

    def __init__(
        self,
        *,
        readiness: ReadinessResult | None = None,
        readiness_error: PsCliError | None = None,
        allow_ingest: bool = False,
    ) -> None:
        """Script this fake's check_readiness() outcome: a result, or an error to raise."""
        self._readiness = readiness
        self._readiness_error = readiness_error
        self._allow_ingest = allow_ingest

    def check_readiness(self) -> ReadinessResult:
        """Return the scripted ReadinessResult, or raise the scripted PsCliError."""
        if self._readiness_error is not None:
            raise self._readiness_error
        assert self._readiness is not None
        return self._readiness

    def ingest_catalog(self, celex: str, *, run_id: str | None = None) -> IngestionResult:
        """Fail the test unless `allow_ingest=True` -- proves the pre-flight check ran first."""
        if not self._allow_ingest:
            msg = (
                f"ingest_catalog must not be called in this test "
                f"(celex={celex!r}, run_id={run_id!r})"
            )
            raise AssertionError(msg)
        return IngestionResult(
            run_id="run-ingest-cli",
            regulatory_instrument_id="ri-cli",
            source="catalog",
            stages=[],
        )


def test_run_ingest_regulation_fails_fast_when_llm_interface_unreachable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-009: LLM Interface unhealthy blocks the command before any POST /ingestions."""
    fake_client = _FakeReadinessGatedIngestClient(
        readiness=ReadinessResult(status="ready", unhealthy_dependencies=["llm_interface"])
    )

    exit_code = run(["ingest", "regulation", "32016R0679"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "LLM Interface" in captured.err
    assert "unavailable" in captured.err


def test_run_ingest_regulation_does_not_block_when_only_cellar_eli_unreachable() -> None:
    """AC-BI-010: cellar_eli alone (LLM Interface healthy) never blocks the command."""
    fake_client = _FakeReadinessGatedIngestClient(
        readiness=ReadinessResult(status="ready", unhealthy_dependencies=["cellar_eli"]),
        allow_ingest=True,
    )

    exit_code = run(["ingest", "regulation", "32016R0679"], client=fake_client)

    assert exit_code == 0


def test_run_ingest_regulation_preflight_fails_closed_distinctly_when_ps_service_unreachable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-012: an unreachable PS Service fails with its own distinct wording.

    Never conflated with the LLM-Interface-unavailable message -- these are two
    different failure modes.
    """
    fake_client = _FakeReadinessGatedIngestClient(
        readiness_error=PsCliError(
            msg="Could not reach PS Service at http://x.",
            hint="check PS_CLI_SERVICE_URL / ps-cli.toml, and that ps-service is running",
        )
    )

    exit_code = run(["ingest", "regulation", "32016R0679"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Could not reach PS Service" in captured.err
    assert "LLM Interface is unavailable" not in captured.err


def test_run_ingest_regulation_preflight_message_never_contains_raw_dependency_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-013: the failure message is the fixed string, never interpolating `readiness`."""
    fake_client = _FakeReadinessGatedIngestClient(
        readiness=ReadinessResult(status="ready", unhealthy_dependencies=["llm_interface"])
    )

    run(["ingest", "regulation", "32016R0679"], client=fake_client)

    captured = capsys.readouterr()
    assert "❌ LLM Interface is unavailable." in captured.err


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

    exit_code = run(["ingest", "regulation", "32016R0679"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Could not reach PS Service at" in captured.err
    assert "Traceback" not in captured.err


def _build_near_misses_handler_with_resource_metadata(
    captured_auth_headers: list[str | None], *, issuer: str, client_id: str
) -> type[BaseHTTPRequestHandler]:
    """Build a handler serving both PS Service's own resource-metadata endpoint and
    `GET /near-misses`, recording each `/near-misses` request's `Authorization`.

    Issue #121: `ensure_valid_access_token` always refreshes on a cache miss
    (AC-BI-003), which requires a real OIDC discovery round trip against *some*
    resource-metadata endpoint -- this one server now plays both roles (mirrors
    `test_integration_auth_full_cycle.py::_build_resource_metadata_handler`'s own
    recipe). Closure-based factory -- `HTTPServer` requires a handler *class*, not
    an instance, so the captured values must be closed over some way other than
    `self`.
    """

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """Silence `BaseHTTPRequestHandler`'s default stderr access log."""

        def do_GET(self) -> None:
            if self.path == "/.well-known/oauth-protected-resource":
                payload = json.dumps(
                    {
                        "resource": "http://ps-service.example",
                        "authorization_servers": [issuer],
                        "scopes_supported": ["openid"],
                        "ps_cli_client_id": client_id,
                    }
                ).encode("utf-8")
            else:
                captured_auth_headers.append(self.headers.get("Authorization"))
                payload = json.dumps({"reviews": []}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return _Handler


def test_resolve_client_attaches_a_freshly_refreshed_bearer_token_to_a_real_business_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_oidc_provider: MockOidcProvider,
    portable_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """Issue #111 Slice 2: closes a real coverage gap on already-shipped AC-BI-003 code.

    Every existing test either injects a fake client (bypassing `_resolve_client`'s
    `PsServiceClient(...)` construction entirely) or uses `client=None` only against
    an unreachable closed port (`test_run_with_unreachable_real_service_returns_one_
    without_crashing` above -- no header assertion possible there). Neither proves
    `_resolve_client` (`cli.py`) actually wires `credential_store`/`context`/
    `auth_override` into the client it builds -- exactly the site of the bug that
    made every authenticated business command silently send no `Authorization`
    header at all (see `_resolve_client`'s own docstring). This test runs a real
    local `http.server.HTTPServer` that records the `Authorization` header of every
    `/near-misses` request, seeds a real refresh_token (obtained via a full
    device-flow login against a real `mock_oidc_provider`) via `build_credential_
    store().set_tokens(...)`, and calls `run(["near-misses", "list"], client=None)`
    -- the exact `client=None` path that forces `_resolve_client` to build a real
    `PsServiceClient` -- pointed at that local server.

    Issue #121: there is no persisted access_token to seed directly any more
    (AC-BI-001) -- `ensure_valid_access_token` always refreshes on a cache miss
    (AC-BI-003), so this test seeds a real refresh_token and lets a genuine refresh
    happen against `mock_oidc_provider`.
    """
    del portable_persistence  # only needed so build_credential_store() has a portable backend
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    client_id = "ps-cli-test-client"

    captured_auth_headers: list[str | None] = []
    server = HTTPServer(
        ("127.0.0.1", 0),
        _build_near_misses_handler_with_resource_metadata(
            captured_auth_headers, issuer=mock_oidc_provider.issuer, client_id=client_id
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
        (tmp_path / "targets.toml").write_text(
            f'current_context = "test"\n\n[contexts.test]\nurl = "{base_url}"\n'
        )

        params = ResolvedAuthParameters(
            issuer=mock_oidc_provider.issuer,
            client_id=client_id,
            scopes=("openid",),
            audience=None,
            device_authorization_endpoint=f"{mock_oidc_provider.base_url}/device_authorization",
            token_endpoint=f"{mock_oidc_provider.base_url}/token",
        )
        device_auth = request_device_authorization(params)
        mock_oidc_provider.complete_device_flow(device_auth.device_code)
        token_response = poll_for_token(
            params, device_auth, sleep=lambda _: pytest.fail("must not sleep")
        )

        credential_store = build_credential_store()
        credential_store.set_tokens(
            "test",
            TokenBundle(
                refresh_token=token_response.refresh_token, issuer=mock_oidc_provider.issuer
            ),
        )

        exit_code = run(["near-misses", "list"], client=None)

        assert exit_code == 0
        assert len(captured_auth_headers) == 1
        assert captured_auth_headers[0] is not None
        assert captured_auth_headers[0].startswith("Bearer ")
    finally:
        server.shutdown()
        thread.join(timeout=5)


# The exact, current string from ps_service/api/routes.py's
# _INTERNAL_NOT_IMPLEMENTED_MESSAGE constant -- confirmed by reading that file
# (read-only reference; ps-cli never imports ps_service). This is what a real,
# unmodified ps-service returns today for `ingest document`, via a 501
# response `PsServiceClient.ingest_internal()` maps to a `PsCliError` carrying
# this exact message (per Increment 14's `TestIngestInternal`).
_INTERNAL_NOT_IMPLEMENTED_MESSAGE = (
    "Internal-document ingestion is not implemented in this walking-skeleton "
    "release; it is tracked in issue #54 (mindovermachine-dev/policy-system)."
)


class _FakeInternalIngestSuccessClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose ingest_internal() succeeds."""

    def check_readiness(self) -> ReadinessResult:
        """Return a healthy default -- this fake's test is not about the pre-flight check."""
        return ReadinessResult(status="ready", unhealthy_dependencies=[])

    def ingest_internal(self, content: dict[str, object]) -> IngestionResult:
        """Return a fixed IngestionResult, ignoring `content`."""
        del content
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

    def check_readiness(self) -> ReadinessResult:
        """Return a healthy default -- this fake's test is not about the pre-flight check."""
        return ReadinessResult(status="ready", unhealthy_dependencies=[])

    def ingest_internal(self, content: dict[str, object]) -> IngestionResult:
        """Raise the PsCliError PsServiceClient.ingest_internal() raises for the real 501."""
        del content
        raise PsCliError(
            msg=f"PS Service reported internal_ingestion_not_implemented: "
            f"{_INTERNAL_NOT_IMPLEMENTED_MESSAGE}",
            hint="run_id: run-internal-501",
        )


# A minimal schema-valid document (issue #54 D7's JSON Schema) -- these two
# tests exercise `cli.run()`'s wiring *past* S1's new local-validation step
# (`ps_cli.intake_validation.validate_local_seed_file`), not that step itself
# (which has its own dedicated tests in `ps-cli/tests/test_intake_validation.py`
# and `ps-cli/tests/modules/test_handlers.py`) -- so the on-disk fixture here
# just needs to pass validation, not be a realistic example.
_MINIMAL_VALID_INTERNAL_SEED_DOCUMENT = {
    "nodes": [
        {
            "label": "RegulatoryInstrument",
            "id": "ENGPRAC-3.0",
            "properties": {
                "title": "Engineering Practices Policy",
                "source_type": "internal",
                "effective_date": "2026-08-01",
                "version": "3.0",
                "status": "active",
            },
        }
    ],
    "edges": [],
}


def _write_valid_internal_seed_fixture(directory: Path, relative_path: str) -> None:
    """Write a schema-valid seed document at `directory / relative_path`."""
    seed_path = directory / relative_path
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    seed_path.write_text(json.dumps(_MINIMAL_VALID_INTERNAL_SEED_DOCUMENT), encoding="utf-8")


def test_ingest_document_prints_run_id_on_mocked_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Covers AC-BI-003's CLI plumbing only, per orchestrator decision 1 (2026-08-30).

    PS Service's real internal pipeline is issue #54, not started; this test
    proves ps-cli's command/argument/REST wiring against a mocked success
    response -- it is NOT proof the real service can ingest an internal
    fixture, and AC-BI-003 must not be marked done on the strength of this
    test alone. The seed fixture is written directly under `tmp_path` (issue
    #91: `ingest document` now takes a plain local filesystem path, not one
    resolved against a configured fixtures root) so local validation passes
    and the call reaches the fake client, unrelated to what this test itself
    proves.
    """
    document_path = tmp_path / "seeds/internal-sop.json"
    _write_valid_internal_seed_fixture(tmp_path, "seeds/internal-sop.json")
    fake_client = _FakeInternalIngestSuccessClient()

    exit_code = run(["ingest", "document", str(document_path)], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "run_id: run-internal-cli" in captured.out


def test_ingest_document_surfaces_real_service_501_as_clean_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What running this command against the real, unmodified ps-service produces today.

    Expected and correct per orchestrator decision 1 -- PS Service's
    `POST /ingestions` with `source: "internal"` currently 501s
    (`internal_ingestion_not_implemented`) until issue #54's backend lands.
    Asserts AC-BI-008's shape: exit 1, the `internal_ingestion_not_implemented`
    message surfaced in stderr, no traceback substring present. The seed
    fixture is written directly under `tmp_path` (issue #91: no more
    fixtures-root indirection) so local validation passes and the call
    reaches the fake client, unrelated to what this test itself proves.
    """
    document_path = tmp_path / "seeds/internal-sop.json"
    _write_valid_internal_seed_fixture(tmp_path, "seeds/internal-sop.json")
    fake_client = _FakeInternalIngest501Client()

    exit_code = run(["ingest", "document", str(document_path)], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "internal_ingestion_not_implemented" in captured.err
    assert _INTERNAL_NOT_IMPLEMENTED_MESSAGE in captured.err
    assert "Traceback" not in captured.err


class _FakeReadinessGatedInternalIngestClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in scripting `check_readiness()`'s outcome for
    `ingest document` (mirrors `_FakeReadinessGatedIngestClient` for `ingest regulation`).

    By default `ingest_internal()` raises `AssertionError` if called -- proving the
    pre-flight check ran first and blocked the command; pass `allow_ingest=True` for the
    one test where the pre-flight check must let the command proceed to a normal,
    successful ingest.
    """

    def __init__(self, *, readiness: ReadinessResult, allow_ingest: bool = False) -> None:
        """Script this fake's check_readiness() outcome, and whether ingest may proceed."""
        self._readiness = readiness
        self._allow_ingest = allow_ingest

    def check_readiness(self) -> ReadinessResult:
        """Return the scripted ReadinessResult."""
        return self._readiness

    def ingest_internal(self, content: dict[str, object]) -> IngestionResult:
        """Fail the test unless `allow_ingest=True` -- proves the pre-flight check ran first."""
        if not self._allow_ingest:
            msg = f"ingest_internal must not be called in this test (content={content!r})"
            raise AssertionError(msg)
        return IngestionResult(
            run_id="run-internal-cli",
            regulatory_instrument_id="ri-internal-cli",
            source="internal",
            stages=[],
        )


def test_ingest_document_fails_fast_when_llm_interface_unreachable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-009: LLM Interface unhealthy blocks `ingest document` before any network call.

    The fixture is schema-valid so local validation passes and the pre-flight check is
    actually reached -- proving it is *this* check, not local validation, that blocks.
    """
    document_path = tmp_path / "seeds/internal-sop.json"
    _write_valid_internal_seed_fixture(tmp_path, "seeds/internal-sop.json")
    fake_client = _FakeReadinessGatedInternalIngestClient(
        readiness=ReadinessResult(status="ready", unhealthy_dependencies=["llm_interface"])
    )

    exit_code = run(["ingest", "document", str(document_path)], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "LLM Interface" in captured.err
    assert "unavailable" in captured.err


def test_ingest_document_does_not_block_when_only_cellar_eli_unreachable(
    tmp_path: Path,
) -> None:
    """AC-BI-010: cellar_eli alone (LLM Interface healthy) never blocks `ingest document`."""
    document_path = tmp_path / "seeds/internal-sop.json"
    _write_valid_internal_seed_fixture(tmp_path, "seeds/internal-sop.json")
    fake_client = _FakeReadinessGatedInternalIngestClient(
        readiness=ReadinessResult(status="ready", unhealthy_dependencies=["cellar_eli"]),
        allow_ingest=True,
    )

    exit_code = run(["ingest", "document", str(document_path)], client=fake_client)

    assert exit_code == 0


def test_ingest_document_still_rejects_invalid_local_file_before_any_network_call(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DD2: local validation still runs before the pre-flight check.

    The bare `_UnusedPsServiceClientMethods()` fake raises `AssertionError` if
    `check_readiness()` (or `ingest_internal()`) is ever called -- so the invalid
    fixture being rejected with a clean `PsCliError`-shaped exit, and no
    `AssertionError` escaping, proves the pre-flight check is never reached for an
    invalid local file.
    """
    invalid_document: dict[str, object] = {
        "nodes": [],
        "edges": [],
        "graph_name": "policy_system",
    }
    document_path = tmp_path / "bad-seed.json"
    document_path.write_text(json.dumps(invalid_document), encoding="utf-8")
    fake_client = _UnusedPsServiceClientMethods()

    exit_code = run(["ingest", "document", str(document_path)], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Traceback" not in captured.err


# --- issue #56 Slice 24: CONFIG_DISPATCH split (PLAN.md §1 D8, critical) ------------------


def test_run_config_set_context_never_constructs_ps_service_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """`config set-context` never calls `load_config()` -- the critical D8 property.

    `targets.toml`'s `current_context` names a context absent from `[contexts]` -- a
    broken value that `load_config()` would raise `PsCliError` on (AC-BI-008). This test
    succeeds anyway (exit 0) with a client fake whose every method raises if called, proving
    both that `load_config()` was never reached (it would have raised) and that
    `PsServiceClient`/the injected client were never touched -- `command in CONFIG_DISPATCH`
    routed straight to `handle_config_set_context`, never the `else` branch (PLAN.md §4
    Slice 24).
    """
    del portable_persistence  # only needed so build_credential_store() has a portable backend
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(tmp_path))
    (tmp_path / "targets.toml").write_text(
        'current_context = "missing"\n\n[contexts.dev]\nurl = "http://127.0.0.1:8000"\n'
    )
    uncallable_client = _UnusedPsServiceClientMethods()

    exit_code = run(
        ["config", "set-context", "prod", "--url", "https://ps.example.com"],
        client=uncallable_client,
    )

    assert exit_code == 0
    targets = load_targets(tmp_path)
    assert targets is not None
    assert targets.contexts["prod"].url == "https://ps.example.com"


def test_run_config_set_context_surfaces_actionable_credential_store_error_via_real_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unusable_persistence: Callable[[str], AlwaysRaisingPersistenceBackend],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-006/007's CLI-level proof: the real dispatch chain, not just the
    `CredentialStore` unit level, surfaces an actionable `PsCliError` naming the
    context but never a token value -- issue #121 replaces the old
    fallback-warning/exit-0 behavior entirely (`FileCredentialStore` is gone,
    AC-BI-008).

    Exercises `run()` -> `CONFIG_DISPATCH` -> `handle_config_set_context` ->
    `build_credential_store` -> `PersistenceCredentialStore` end to end -- a wiring bug
    anywhere in that chain would fail this test even though every narrower slice
    still passes on its own. `set-context` is the only command that ever touches
    `CredentialStore` (D13's unconditional `delete_tokens`) -- `use-context`/
    `get-contexts` have no `credential_store` param by design, so this single
    command's proof fully covers AC-BI-006/007's "every command that reads or
    writes it" wording for this issue's scope.

    `unusable_persistence` (`conftest.py`) monkeypatches the production persistence
    factory to raise a bare `OSError` -- reproducing "the failure is a raw OS-level
    exception, not a reshaped library-specific error type" (TASK.md issue #121, the
    original motivating case was a raw `win32ctypes.pywin32.pywintypes.error`) --
    rather than any narrower library-specific exception subclass.
    """
    del unusable_persistence  # only needed so build_credential_store() hits an unusable backend
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(tmp_path))
    uncallable_client = _UnusedPsServiceClientMethods()

    exit_code = run(
        ["config", "set-context", "prod", "--url", "https://ps.example.com"],
        client=uncallable_client,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "❌" in captured.err
    assert "prod" in captured.err
    assert "credentials.toml" not in captured.err


# --- issue #56 Slice 26: --context flag wiring + AC-BI-005 end-to-end proof --------------


def test_ac_bi_005_set_context_then_use_context_drives_subsequent_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """AC-BI-005's literal scenario: `set-context` then `use-context` drives resolution.

    CHANGES.md F3: the only assertion mechanism used is a direct, independent
    `load_config()` check -- deterministic, no network, no `PsServiceClient`
    mocking/capture -- exactly what every real command's own `load_config()` call would
    resolve to next. The two `run()` calls use an uncallable client fake (this is `config
    set-context`/`use-context`, neither of which ever touches `PsServiceClient` -- D8).
    """
    del portable_persistence  # only needed so build_credential_store() has a portable backend
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    uncallable_client = _UnusedPsServiceClientMethods()

    set_exit_code = run(
        ["config", "set-context", "prod", "--url", "https://ps.example.com"],
        client=uncallable_client,
    )
    use_exit_code = run(["config", "use-context", "prod"], client=uncallable_client)

    assert set_exit_code == 0
    assert use_exit_code == 0
    assert load_config(context=None, config_dir=tmp_path).service_url == "https://ps.example.com"


# --- issue #56 Slice 27: --context single-invocation override, AC-BI-006 end-to-end ------


def test_ac_bi_006_context_param_overrides_for_one_call_only_not_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--context`'s resolved value overrides `current_context` for one call only.

    Continues Slice 26's fixture: two contexts (`dev`, `prod`), `current_context="prod"`.
    `load_config(context="dev", ...)` resolves to `dev`'s URL for that one call; a second,
    independent `load_config()` call with no `context` param (simulating the next
    invocation with no `--context` given) still resolves to `prod`'s URL -- proving the
    override is per-invocation only, never persisted back into `targets.toml`.
    """
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    uncallable_client = _UnusedPsServiceClientMethods()
    run(
        ["config", "set-context", "dev", "--url", "http://ctx-dev:9000"],
        client=uncallable_client,
    )
    run(
        ["config", "set-context", "prod", "--url", "https://ps.example.com"],
        client=uncallable_client,
    )
    run(["config", "use-context", "prod"], client=uncallable_client)

    overridden = load_config(context="dev", config_dir=tmp_path)
    unoverridden = load_config(config_dir=tmp_path)

    assert overridden.service_url == "http://ctx-dev:9000"
    assert unoverridden.service_url == "https://ps.example.com"


def test_parser_context_flag_before_subcommand_parses_correctly() -> None:
    """`ps-cli --context dev get catalog` (flag before subcommand) parses `args.context`.

    The issue's own literal example (PLAN.md §1 D7's shared-parent-parser `SUPPRESS`
    mechanism, §0.4) -- proves the flag survives the subparser dispatch's namespace copy
    when given before the subcommand name, not only after it.
    """
    args = build_parser().parse_args(["--context", "dev", "get", "catalog"])

    assert args.context == "dev"


# --- issue #56 Slice 28: get-contexts, real dispatch (AC-BI-007 end-to-end proof) -------


def test_run_config_get_contexts_never_constructs_ps_service_client_and_prints_contexts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`config get-contexts` reaches `handle_config_get_contexts()` via the real
    `run()` -> `CONFIG_DISPATCH` chain (AC-BI-007), never constructing a `PsServiceClient`
    -- mirroring Slice 24's `set-context` proof (D8). A broken `current_context` (naming a
    context absent from `[contexts]`, which `load_config()` would raise on -- AC-BI-008)
    is deliberately present, proving `get-contexts` stays usable exactly when it is most
    needed: diagnosing a broken `targets.toml`.
    """
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(tmp_path))
    (tmp_path / "targets.toml").write_text(
        'current_context = "missing"\n\n'
        '[contexts.dev]\nurl = "http://ctx-dev:9000"\n\n'
        '[contexts.prod]\nurl = "https://ps.example.com"\n'
    )
    uncallable_client = _UnusedPsServiceClientMethods()

    exit_code = run(["config", "get-contexts"], client=uncallable_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "dev" in captured.out
    assert "http://ctx-dev:9000" in captured.out
    assert "prod" in captured.out
    assert "https://ps.example.com" in captured.out


def _write_catalog_fixture(repo_path: Path) -> None:
    (repo_path / "catalog.json").write_text(
        json.dumps(
            [
                {
                    "instrument_id": "CRA-1.0",
                    "title": "Cyber Resilience Act",
                    "source_type": "external",
                    "jurisdiction": "EU",
                }
            ]
        ),
        encoding="utf-8",
    )


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


def test_run_get_catalog_never_constructs_client_but_resolves_curated_repo_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`get catalog` reads `curated_repo_path` via `load_config()` but never touches
    `PsServiceClient` at all (D13) -- proven via an uncallable client fake, mirroring
    `test_run_config_set_context_never_constructs_ps_service_client`'s own proof shape:
    if `run()` ever called a method on `uncallable_client`, this test would fail with an
    uncaught `AssertionError`, not a graceful exit code.
    """
    curated_repo_path = tmp_path / "curated-content"
    curated_repo_path.mkdir()
    _write_catalog_fixture(curated_repo_path)
    monkeypatch.setenv("PS_CLI_CURATED_REPO_PATH", str(curated_repo_path))
    uncallable_client = _UnusedPsServiceClientMethods()

    exit_code = run(["get", "catalog"], client=uncallable_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "CRA-1.0" in captured.out
    assert "Cyber Resilience Act" in captured.out
    assert "external" in captured.out
    assert "EU" in captured.out


def test_get_catalog_unaffected_by_llm_interface_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-008: `get catalog` succeeds unaffected while LLM Interface is down.

    Mirrors `test_run_get_catalog_never_constructs_client_but_resolves_curated_repo_path`:
    `get catalog` never receives a real `PsServiceClient` at all (D13), so it is
    structurally unaffected by any PS Service dependency state, LLM Interface included.
    Proven via an uncallable client fake -- if `run()` ever called a method on
    `uncallable_client`, this test would fail with an uncaught `AssertionError`.
    """
    curated_repo_path = tmp_path / "curated-content"
    curated_repo_path.mkdir()
    _write_catalog_fixture(curated_repo_path)
    monkeypatch.setenv("PS_CLI_CURATED_REPO_PATH", str(curated_repo_path))
    uncallable_client = _UnusedPsServiceClientMethods()

    exit_code = run(["get", "catalog"], client=uncallable_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "CRA-1.0" in captured.out
    assert "Cyber Resilience Act" in captured.out
    assert "external" in captured.out
    assert "EU" in captured.out


class _FakeRestoreSuccessClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose restore_instrument() succeeds."""

    def restore_instrument(self, artifact: CuratedArtifact) -> RestorationResult:
        """Return a fixed RestorationResult, echoing the artifact's own instrument id."""
        return RestorationResult(
            instrument_id=artifact.manifest.instrument_id,
            stages=[RestorationStageOutcome(stage="verified", status="succeeded")],
        )


def test_run_restore_instrument_prints_instrument_id_on_mocked_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The full parser -> dispatch -> handler -> client wiring for `restore instrument`."""
    curated_repo_path = tmp_path / "curated-content"
    curated_repo_path.mkdir()
    _write_instrument_fixture(curated_repo_path, "CRA-1.0")
    monkeypatch.setenv("PS_CLI_CURATED_REPO_PATH", str(curated_repo_path))
    fake_client = _FakeRestoreSuccessClient()

    exit_code = run(["restore", "instrument", "CRA-1.0"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "instrument_id: CRA-1.0" in captured.out
    assert "verified: succeeded" in captured.out


def test_run_restore_instrument_missing_local_artifact_exits_one_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing local instrument directory surfaces as a clean PsCliError, exit 1."""
    curated_repo_path = tmp_path / "curated-content"
    curated_repo_path.mkdir()
    monkeypatch.setenv("PS_CLI_CURATED_REPO_PATH", str(curated_repo_path))
    uncallable_client = _UnusedPsServiceClientMethods()

    exit_code = run(["restore", "instrument", "MISSING-1.0"], client=uncallable_client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "curated instrument directory not found" in captured.err
    assert "Traceback" not in captured.err


# --- issue #71, new S3 (CHANGES.md A2): `ps-cli export instrument` end to end ----------------

_EXPORT_MANIFEST = ExportManifest(
    instrument_id="CRA-1.0",
    celex="32024R2847",
    title="Cyber Resilience Act",
    short_name="CRA",
    version="1.0",
    source_type="external",
    jurisdiction="EU",
    schema_version="1.0.0",
    exported_at="2026-09-04T00:00:00Z",
    baseline_sha256="a" * 64,
    native_sha256="b" * 64,
)

_EXPORT_BASELINE_BLOB = b'{"baseline": true}'
_EXPORT_NATIVE_BLOB = b'{"native": true}'


class _FakeExportSuccessClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose export_instrument() succeeds."""

    def export_instrument(self, instrument_id: str) -> ExportResult:
        """Return a fixed ExportResult, echoing the given instrument id."""
        return ExportResult(
            instrument_id=instrument_id,
            manifest=_EXPORT_MANIFEST,
            baseline_blob_base64=base64.b64encode(_EXPORT_BASELINE_BLOB).decode("ascii"),
            native_blob_base64=base64.b64encode(_EXPORT_NATIVE_BLOB).decode("ascii"),
            stages=[ExportStageOutcome(stage="serialized", status="succeeded")],
        )


def test_run_export_instrument_writes_files_and_prints_summary_on_mocked_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The full parser -> dispatch -> handler -> client wiring for `export instrument`.

    Uses an `external`-source fake manifest -- the internal-source notice (AC-BI-014)
    is a later slice's own dedicated test, not this one's concern either way.
    """
    fake_client = _FakeExportSuccessClient()

    exit_code = run(["export", "instrument", "CRA-1.0", str(tmp_path)], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 0

    baseline_path = tmp_path / "baseline.json"
    native_path = tmp_path / "native.json"
    manifest_path = tmp_path / "manifest.json"
    assert baseline_path.read_bytes() == _EXPORT_BASELINE_BLOB
    assert native_path.read_bytes() == _EXPORT_NATIVE_BLOB
    manifest_content = json.loads(manifest_path.read_text())
    assert manifest_content == {
        "instrument_id": "CRA-1.0",
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

    assert "instrument_id: CRA-1.0" in captured.out
    assert "serialized: succeeded" in captured.out
    assert str(baseline_path) in captured.out
    assert str(native_path) in captured.out
    assert str(manifest_path) in captured.out


def test_run_export_instrument_defaults_destination_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-006: omitting `destination` writes the three files to the operator's cwd.

    Mirrors `test_run_export_instrument_writes_files_and_prints_summary_on_mocked_success`
    but omits the `destination` positional entirely, relying on
    `handle_export_instrument`'s own `destination or Path.cwd()` resolution.
    """
    monkeypatch.chdir(tmp_path)
    fake_client = _FakeExportSuccessClient()

    exit_code = run(["export", "instrument", "CRA-1.0"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert (tmp_path / "baseline.json").read_bytes() == _EXPORT_BASELINE_BLOB
    assert (tmp_path / "native.json").read_bytes() == _EXPORT_NATIVE_BLOB
    assert (tmp_path / "manifest.json").exists()
    assert "instrument_id: CRA-1.0" in captured.out


def test_run_export_instrument_nonexistent_destination_fails_before_calling_client(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-008: a destination that doesn't exist fails fast, before PS Service is called.

    Uses the bare `_UnusedPsServiceClientMethods()` fake -- if `handle_export_instrument`
    ever called `export_instrument()` before the destination check, this test would fail
    with an uncaught `AssertionError`, not a graceful exit code.
    """
    missing_destination = tmp_path / "nonexistent"
    uncallable_client = _UnusedPsServiceClientMethods()

    exit_code = run(
        ["export", "instrument", "CRA-1.0", str(missing_destination)], client=uncallable_client
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert str(missing_destination) in captured.err
    assert "💡" in captured.err
    assert "Traceback" not in captured.err


def test_run_export_instrument_unwritable_destination_fails_before_calling_client(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-008: a destination that exists but isn't writable fails fast, same as above.

    Uses a dedicated subdirectory of `tmp_path` (never `tmp_path` itself) so the
    permission-bit change is scoped to a directory this test controls end to end; the
    `finally` block restores it to a writable mode before pytest's own `tmp_path`
    teardown runs, so no permission-broken temp dir is left behind.
    """
    unwritable_destination = tmp_path / "readonly"
    unwritable_destination.mkdir()
    unwritable_destination.chmod(0o500)
    uncallable_client = _UnusedPsServiceClientMethods()

    try:
        exit_code = run(
            ["export", "instrument", "CRA-1.0", str(unwritable_destination)],
            client=uncallable_client,
        )
    finally:
        unwritable_destination.chmod(0o700)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert str(unwritable_destination) in captured.err
    assert "💡" in captured.err
    assert "Traceback" not in captured.err


_EXPORT_MANIFEST_V2 = ExportManifest(
    instrument_id="CRA-2.0",
    celex="32024R2847",
    title="Cyber Resilience Act (Amended)",
    short_name="CRA",
    version="2.0",
    source_type="external",
    jurisdiction="EU",
    schema_version="1.0.0",
    exported_at="2026-09-16T00:00:00Z",
    baseline_sha256="c" * 64,
    native_sha256="d" * 64,
)

_EXPORT_BASELINE_BLOB_V2 = b'{"baseline": true, "amended": true}'
_EXPORT_NATIVE_BLOB_V2 = b'{"native": true, "amended": true}'


class _FakeExportSuccessClientV2(_UnusedPsServiceClientMethods):
    """A second duck-typed `export_instrument()` fake returning different content than V1.

    Simulates a re-export after the source instrument changed (AC-BI-010).
    """

    def export_instrument(self, instrument_id: str) -> ExportResult:
        """Return a fixed ExportResult distinct from `_FakeExportSuccessClient`'s."""
        return ExportResult(
            instrument_id=instrument_id,
            manifest=_EXPORT_MANIFEST_V2,
            baseline_blob_base64=base64.b64encode(_EXPORT_BASELINE_BLOB_V2).decode("ascii"),
            native_blob_base64=base64.b64encode(_EXPORT_NATIVE_BLOB_V2).decode("ascii"),
            stages=[ExportStageOutcome(stage="serialized", status="succeeded")],
        )


def test_run_export_instrument_overwrites_prior_export_deterministically(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-010: re-exporting to the same destination fully replaces the prior artifact.

    Runs the full `run(...)` dispatch twice against the same `tmp_path`, with two fake
    clients returning two *different* `ExportResult`s (different instrument id, blob
    content, and manifest fields -- simulating a re-export after the source instrument
    changed). After the second run, none of the first run's bytes may survive in any of
    the three written files -- not as leftover trailing bytes, not merged into the JSON,
    nowhere.
    """
    exit_code_1 = run(
        ["export", "instrument", "CRA-1.0", str(tmp_path)], client=_FakeExportSuccessClient()
    )
    assert exit_code_1 == 0
    capsys.readouterr()

    exit_code_2 = run(
        ["export", "instrument", "CRA-2.0", str(tmp_path)], client=_FakeExportSuccessClientV2()
    )
    assert exit_code_2 == 0
    capsys.readouterr()

    baseline_path = tmp_path / "baseline.json"
    native_path = tmp_path / "native.json"
    manifest_path = tmp_path / "manifest.json"

    assert baseline_path.read_bytes() == _EXPORT_BASELINE_BLOB_V2
    assert native_path.read_bytes() == _EXPORT_NATIVE_BLOB_V2
    manifest_content = json.loads(manifest_path.read_text())
    assert manifest_content == {
        "instrument_id": "CRA-2.0",
        "celex": "32024R2847",
        "title": "Cyber Resilience Act (Amended)",
        "short_name": "CRA",
        "version": "2.0",
        "source_type": "external",
        "jurisdiction": "EU",
        "schema_version": "1.0.0",
        "exported_at": "2026-09-16T00:00:00Z",
        "baseline_sha256": "c" * 64,
        "native_sha256": "d" * 64,
    }

    # No trace of the first run's content survives anywhere in any of the three files.
    assert _EXPORT_BASELINE_BLOB not in baseline_path.read_bytes()
    assert _EXPORT_NATIVE_BLOB not in native_path.read_bytes()
    first_run_manifest_text = manifest_path.read_text()
    assert "CRA-1.0" not in first_run_manifest_text
    assert "a" * 64 not in first_run_manifest_text
    assert "b" * 64 not in first_run_manifest_text
    assert 'Cyber Resilience Act"' not in first_run_manifest_text


# --- issue #71, S14 (AC-BI-014): internal-source confidentiality notice -----------------------

_EXPORT_MANIFEST_INTERNAL = ExportManifest(
    instrument_id="INTERNAL-POLICY-1.0",
    celex=None,
    title="Internal Remote Work Policy",
    short_name="RWP",
    version="1.0",
    source_type="internal",
    jurisdiction=None,
    schema_version="1.0.0",
    exported_at="2026-09-16T00:00:00Z",
    baseline_sha256="e" * 64,
    native_sha256="f" * 64,
)


class _FakeExportSuccessClientInternal(_UnusedPsServiceClientMethods):
    """A duck-typed `export_instrument()` fake whose manifest's `source_type` is `internal`."""

    def export_instrument(self, instrument_id: str) -> ExportResult:
        """Return a fixed ExportResult carrying `_EXPORT_MANIFEST_INTERNAL`."""
        return ExportResult(
            instrument_id=instrument_id,
            manifest=_EXPORT_MANIFEST_INTERNAL,
            baseline_blob_base64=base64.b64encode(_EXPORT_BASELINE_BLOB).decode("ascii"),
            native_blob_base64=base64.b64encode(_EXPORT_NATIVE_BLOB).decode("ascii"),
            stages=[ExportStageOutcome(stage="serialized", status="succeeded")],
        )


def test_run_export_instrument_internal_source_prints_confidentiality_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-014: an internal-source export prints an explicit confidentiality notice.

    Exact wording is non-load-bearing (PLAN.md S14) -- only that a notice mentioning
    both "internal" and "confidential" appears on stdout after the usual summary lines.
    """
    fake_client = _FakeExportSuccessClientInternal()

    exit_code = run(
        ["export", "instrument", "INTERNAL-POLICY-1.0", str(tmp_path)], client=fake_client
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "NOTICE" in captured.out
    assert "internal" in captured.out
    assert "confidential" in captured.out


def test_run_export_instrument_external_source_prints_no_confidentiality_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-014: an external-source export's stdout is unchanged -- no notice, no regression.

    Byte-for-byte identical to
    `test_run_export_instrument_writes_files_and_prints_summary_on_mocked_success`'s own
    stdout assertions -- proving AC-BI-014's addition is a no-op for the common case.
    """
    fake_client = _FakeExportSuccessClient()

    exit_code = run(["export", "instrument", "CRA-1.0", str(tmp_path)], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 0

    baseline_path = tmp_path / "baseline.json"
    native_path = tmp_path / "native.json"
    manifest_path = tmp_path / "manifest.json"
    assert "instrument_id: CRA-1.0" in captured.out
    assert "serialized: succeeded" in captured.out
    assert str(baseline_path) in captured.out
    assert str(native_path) in captured.out
    assert str(manifest_path) in captured.out
    assert "NOTICE" not in captured.out


# --- issue #68 Slice 10: `ps-cli get health` end to end, through cli.run() -------------------


class _FakeHealthClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in with scripted check_health()/check_readiness().

    Mirrors `ps-cli/tests/modules/test_handlers.py::_FakeHealthClient` (Slices 8-9) at the
    `cli.run()` layer instead of calling `handle_get_health()` directly -- this file exercises
    the full parser -> dispatch -> handler -> client wire-up, not just the handler in
    isolation.
    """

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


def test_run_get_health_returns_zero_on_happy_path(capsys: pytest.CaptureFixture[str]) -> None:
    """`run(["get", "health"], client=<fully healthy fake>)` returns 0 and prints the three summary
    lines to stdout (AC-BI-004 end-to-end, D11).
    """
    fake_client = _FakeHealthClient(
        health_status="alive",
        readiness=ReadinessResult(status="ready", unhealthy_dependencies=[]),
    )

    exit_code = run(["get", "health"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "reachable: yes" in captured.out
    assert "health: alive" in captured.out
    assert "ready: ready" in captured.out


def test_run_get_health_returns_one_with_distinct_message_when_not_ready(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`run(["get", "health"], client=<not-ready fake>)` returns 1; stderr names the unhealthy
    dependency and is textually distinct from the unreachable-target wording (AC-BI-001,
    AC-BI-007, AC-BI-008 end-to-end).
    """
    fake_client = _FakeHealthClient(
        health_status="alive",
        readiness=ReadinessResult(status="not_ready", unhealthy_dependencies=["falkordb"]),
    )

    exit_code = run(["get", "health"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "not ready" in captured.err
    assert "falkordb" in captured.err
    assert "Could not reach" not in captured.err


def test_run_get_health_with_unreachable_real_service_returns_one_without_crashing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-009's real-network proof for `get health`, mirroring `test_run_with_unreachable_
    real_service_returns_one_without_crashing`'s exact pattern (PLAN.md §4 Slice 10):
    `run(["get", "health"])` with `client=None` builds a real `PsServiceClient` from
    `PS_CLI_SERVICE_URL`, pointed at a definitely-closed local port (bind a socket, close it,
    reuse the freed port number -- a small accepted TOCTOU flake risk per PLAN.md, not
    engineered away). Asserts an actionable message on stderr, exit code 1, and -- by simply
    not wrapping the call in `pytest.raises` -- that no exception escapes `run()`.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setenv("PS_CLI_SERVICE_URL", f"http://127.0.0.1:{port}")

    exit_code = run(["get", "health"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Could not reach PS Service at" in captured.err
    assert "Traceback" not in captured.err


def test_run_get_health_with_context_flag_resolves_named_targets_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """AC-BI-005/AC-BI-006 end-to-end for `get health`, per CHANGES.md M2 (the only valid proof
    mechanism for this slice): an independent `load_config(context=..., config_dir=...)`
    check performed after `config set-context`/`config use-context` calls against an
    isolated `PS_CLI_CONFIG_DIR`, mirroring `test_ac_bi_005_set_context_then_use_context_
    drives_subsequent_resolution`'s exact pattern (issue #56 Slice 26).

    Deliberately does **not** call `run(["get", "health", "--context", <name>], client=<fake>)`:
    `_resolve_client()` (`cli.py:42-55`) returns an injected `client` unchanged before
    `load_config(context=...)` is ever reached, so a fake client's presence would make such a
    test pass regardless of whether `--context` resolution actually worked -- it would prove
    nothing about `--context`. `_resolve_client()`'s own `load_config(context=context).
    service_url` call is exactly what this test proves resolves correctly; `get health`'s
    `DISPATCH` entry reaches that same generic code path as every other client-backed
    command (D9), so this proof transfers to `get health` without needing to invoke it at all.
    """
    del portable_persistence  # only needed so build_credential_store() has a portable backend
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    uncallable_client = _UnusedPsServiceClientMethods()

    set_exit_code = run(
        ["config", "set-context", "prod", "--url", "https://ps.example.com"],
        client=uncallable_client,
    )
    use_exit_code = run(["config", "use-context", "prod"], client=uncallable_client)

    assert set_exit_code == 0
    assert use_exit_code == 0
    assert load_config(context=None, config_dir=tmp_path).service_url == "https://ps.example.com"


class _FakeCheckClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in with a scripted run_change_check() (issue #73)."""

    def __init__(self, result: ChangeCheckResult) -> None:
        """Script this fake's `run_change_check()` return value."""
        self._result = result

    def check_readiness(self) -> ReadinessResult:
        """Return a healthy default -- this fake's tests are not about the pre-flight check."""
        return ReadinessResult(status="ready", unhealthy_dependencies=[])

    def run_change_check(self) -> ChangeCheckResult:
        """Return the scripted result."""
        return self._result


class _FakeReadinessGatedCheckClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in scripting `check_readiness()`'s outcome for
    `check regulations` (mirrors `_FakeReadinessGatedIngestClient` for `ingest regulation`).

    By default `run_change_check()` raises `AssertionError` if called -- proving the
    pre-flight check ran first and blocked the command; pass `allow_check=True` for the
    one test where the pre-flight check must let the command proceed.
    """

    def __init__(self, *, readiness: ReadinessResult, allow_check: bool = False) -> None:
        """Script this fake's check_readiness() outcome, and whether the sweep may proceed."""
        self._readiness = readiness
        self._allow_check = allow_check

    def check_readiness(self) -> ReadinessResult:
        """Return the scripted ReadinessResult."""
        return self._readiness

    def run_change_check(self) -> ChangeCheckResult:
        """Fail the test unless `allow_check=True` -- proves the pre-flight check ran first."""
        if not self._allow_check:
            raise AssertionError("run_change_check must not be called in this test")
        return ChangeCheckResult(run_id="run-check-cli", instruments=[])


def test_check_regulations_fails_fast_when_llm_interface_unreachable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-009: LLM Interface unhealthy blocks `check regulations` before any pipeline call."""
    fake_client = _FakeReadinessGatedCheckClient(
        readiness=ReadinessResult(status="ready", unhealthy_dependencies=["llm_interface"])
    )

    exit_code = run(["check", "regulations"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "LLM Interface" in captured.err
    assert "unavailable" in captured.err


def test_check_regulations_does_not_block_when_only_cellar_eli_unreachable() -> None:
    """AC-BI-010: cellar_eli alone (LLM Interface healthy) never blocks `check regulations`."""
    fake_client = _FakeReadinessGatedCheckClient(
        readiness=ReadinessResult(status="ready", unhealthy_dependencies=["cellar_eli"]),
        allow_check=True,
    )

    exit_code = run(["check", "regulations"], client=fake_client)

    assert exit_code == 0


def test_run_check_regulations_returns_zero_on_empty_sweep(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`run(["check", "regulations"], client=<empty-sweep fake>)` returns 0; stdout has both lines
    (issue #73, PLAN.md §4 Slice 1).
    """
    fake_client = _FakeCheckClient(ChangeCheckResult(run_id="r1", instruments=[]))

    exit_code = run(["check", "regulations"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "run_id: r1" in captured.out
    assert "no tracked instruments" in captured.out


def test_run_check_regulations_prints_run_id_and_returns_zero_end_to_end(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`run(["check", "regulations"], client=<multi-bucket fake>)` -> `0`; stdout has the run id
    first, proving ps-cli can display any outcome bucket from Slice 2 onward
    (issue #73, PLAN.md §4 Slice 2, CHANGES.md's re-sequencing) -- closing the
    display gap for Slices 3-5 structurally, the same way Slice 7 structurally
    closes AC-BI-009.
    """
    fake_client = _FakeCheckClient(
        ChangeCheckResult(
            run_id="sweep-1",
            instruments=[
                InstrumentCheckOutcome(
                    "CRA-1.0", "amendment_reingested", "-> CRA-1.0 (superseded)", "ingest-run-1"
                ),
                InstrumentCheckOutcome("GDPR-1.0", "current", None, None),
            ],
        )
    )

    exit_code = run(["check", "regulations"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    lines = captured.out.splitlines()
    assert lines[0] == "run_id: sweep-1"
    assert "CRA-1.0: amendment_reingested (-> CRA-1.0 (superseded))" in lines
    assert "GDPR-1.0: current" in lines


# --- issue #82 Slice 8: `--version --context` (AC-BI-006, AC-BI-007) --------------------


def test_parser_version_flag_accepts_context_before_and_after() -> None:
    """`--version` and `--context` compose in either order (both are top-level shared flags).

    Direct parse-level proof: no existing test exercises `--context` alongside
    `--version` specifically, only alongside a subcommand.
    """
    after = build_parser().parse_args(["--version", "--context", "dev"])
    before = build_parser().parse_args(["--context", "dev", "--version"])

    assert after.context == "dev"
    assert after.version is True
    assert before.context == "dev"
    assert before.version is True


def test_ac_bi_006_version_context_flag_targets_the_named_contexts_url_for_one_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--version --context dev` queries `dev`'s URL for this invocation only.

    No implementation change needed for this slice (PLAN.md §5 Slice 8) --
    `_report_service_version` already threads `args`/`client` through the
    existing `_resolve_client`, unchanged. Mirrors `test_ac_bi_006_context_param_
    overrides_for_one_call_only_not_persisted`'s style, adapted to prove the
    property through `--version`'s own printed output rather than a second
    `load_config()` call: bind-then-close two separate local sockets (`dev`'s and
    `prod`'s freed ports), `use-context prod`, then `run(["--version", "--context",
    "dev"])` with `client=None` -- the "unavailable" line's embedded URL must
    contain `dev`'s port, not `prod`'s.
    """
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    uncallable_client = _UnusedPsServiceClientMethods()

    dev_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    dev_probe.bind(("127.0.0.1", 0))
    dev_port = dev_probe.getsockname()[1]
    dev_probe.close()

    prod_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    prod_probe.bind(("127.0.0.1", 0))
    prod_port = prod_probe.getsockname()[1]
    prod_probe.close()

    run(
        ["config", "set-context", "dev", "--url", f"http://127.0.0.1:{dev_port}"],
        client=uncallable_client,
    )
    run(
        ["config", "set-context", "prod", "--url", f"http://127.0.0.1:{prod_port}"],
        client=uncallable_client,
    )
    run(["config", "use-context", "prod"], client=uncallable_client)

    exit_code = run(["--version", "--context", "dev"], client=None)

    captured = capsys.readouterr()
    assert exit_code == 0
    service_line = captured.out.splitlines()[1]
    assert f":{dev_port}" in service_line
    assert f":{prod_port}" not in service_line
