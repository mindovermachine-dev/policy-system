"""Tests for ps_cli.cli: run() (which wires ps_cli.modules.parser.build_parser() and
ps_cli.modules.handlers.DISPATCH together), and __main__.py's AST shape.

PLAN.md §3 Increments 10, 12, 13.
"""

from __future__ import annotations

import ast
import json
import socket
from pathlib import Path
from typing import TYPE_CHECKING

import keyring.errors
import pytest

from ps_cli.cli import run
from ps_cli.config import load_config
from ps_cli.errors import PsCliError
from ps_cli.models import (
    IngestionResult,
    ReadinessResult,
    RegulationsResult,
    RestorationResult,
    RestorationStageOutcome,
)
from ps_cli.modules.parser import build_parser
from ps_cli.targets import load_targets

if TYPE_CHECKING:
    from ps_cli.catalog_repo import CuratedArtifact

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


# --- issue #56 Slice 24: CONFIG_DISPATCH split (PLAN.md §1 D8, critical) ------------------


def test_run_config_set_context_never_constructs_ps_service_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(tmp_path))
    (tmp_path / "targets.toml").write_text(
        'current_context = "missing"\n\n[contexts]\ndev = "http://127.0.0.1:8000"\n'
    )
    uncallable_client = _UnusedPsServiceClientMethods()

    exit_code = run(
        ["config", "set-context", "prod", "--url", "https://ps.example.com"],
        client=uncallable_client,
    )

    assert exit_code == 0
    targets = load_targets(tmp_path)
    assert targets is not None
    assert targets.contexts["prod"] == "https://ps.example.com"


def _raise_no_keyring_error(*args: object, **kwargs: object) -> None:
    """Unconditionally raise `NoKeyringError` -- a stand-in for `keyring`'s module-level
    `get_password`/`set_password`/`delete_password` functions when no OS backend exists.
    """
    del args, kwargs
    raise keyring.errors.NoKeyringError("no backend")


def test_run_config_set_context_prints_fallback_warning_on_stderr_via_real_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-011's CLI-level proof (CHANGES.md F4): the real dispatch chain, not just the
    `CredentialStore` unit level, ends up warning on stderr.

    Exercises `run()` -> `CONFIG_DISPATCH` -> `handle_config_set_context` ->
    `build_credential_store` -> `KeyringCredentialStore` -> `FileCredentialStore` end to
    end -- a wiring bug anywhere in that chain would fail this test even though every
    narrower slice (15-23.5) still passes on its own. `set-context` is the only one of this
    issue's three new commands that ever touches `CredentialStore` (D13's unconditional
    `delete_credential`) -- `use-context`/`list-contexts` have no `credential_store` param
    by design, so this single command's proof fully covers AC-BI-011's "every command that
    reads or writes it" wording for this issue's scope.

    **Deviation from CHANGES.md F4's literal mechanism text**, flagged here (see
    `IMPL_SLICE_22-24.md` for the full writeup): F4 says to
    `monkeypatch.setattr("ps_cli.credentials.keyring", _AlwaysNoKeyringErrorBackend())` --
    replacing the whole `keyring` module-level symbol `credentials.py` imports. That
    literal mechanism was tried first and found to break `KeyringCredentialStore`'s own
    `except keyring.errors.PasswordDeleteError`/`except keyring.errors.KeyringError`
    clauses: both `import keyring` and `import keyring.errors` in `credentials.py` bind the
    *same* module-level name `keyring`, so replacing it with a fake object that has no
    `.errors` attribute makes exception-type evaluation itself raise `AttributeError` the
    first time `delete_credential()` runs -- confirmed by running the literal mechanism and
    observing exactly this crash. The fix used here monkeypatches only the three
    module-level *functions* `build_credential_store()`'s default `keyring_backend=keyring`
    actually calls (`keyring.get_password`/`set_password`/`delete_password`) to raise
    `NoKeyringError`, leaving the `keyring`/`keyring.errors` symbols themselves untouched --
    same portable, no-real-environment-dependency intent F4 describes, without the crash.
    """
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("keyring.get_password", _raise_no_keyring_error)
    monkeypatch.setattr("keyring.set_password", _raise_no_keyring_error)
    monkeypatch.setattr("keyring.delete_password", _raise_no_keyring_error)
    uncallable_client = _UnusedPsServiceClientMethods()

    exit_code = run(
        ["config", "set-context", "prod", "--url", "https://ps.example.com"],
        client=uncallable_client,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "no OS keyring backend available" in captured.err
    assert str(tmp_path / "credentials.toml") in captured.err


# --- issue #56 Slice 26: --context flag wiring + AC-BI-005 end-to-end proof --------------


def test_ac_bi_005_set_context_then_use_context_drives_subsequent_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-BI-005's literal scenario: `set-context` then `use-context` drives resolution.

    CHANGES.md F3: the only assertion mechanism used is a direct, independent
    `load_config()` check -- deterministic, no network, no `PsServiceClient`
    mocking/capture -- exactly what every real command's own `load_config()` call would
    resolve to next. The two `run()` calls use an uncallable client fake (this is `config
    set-context`/`use-context`, neither of which ever touches `PsServiceClient` -- D8).
    """
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
    """`ps-cli --context dev regulations list` (flag before subcommand) parses `args.context`.

    The issue's own literal example (PLAN.md §1 D7's shared-parent-parser `SUPPRESS`
    mechanism, §0.4) -- proves the flag survives the subparser dispatch's namespace copy
    when given before the subcommand name, not only after it.
    """
    args = build_parser().parse_args(["--context", "dev", "regulations", "list"])

    assert args.context == "dev"


# --- issue #56 Slice 28: list-contexts, real dispatch (AC-BI-007 end-to-end proof) -------


def test_run_config_list_contexts_never_constructs_ps_service_client_and_prints_contexts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`config list-contexts` reaches `handle_config_list_contexts()` via the real
    `run()` -> `CONFIG_DISPATCH` chain (AC-BI-007), never constructing a `PsServiceClient`
    -- mirroring Slice 24's `set-context` proof (D8). A broken `current_context` (naming a
    context absent from `[contexts]`, which `load_config()` would raise on -- AC-BI-008)
    is deliberately present, proving `list-contexts` stays usable exactly when it is most
    needed: diagnosing a broken `targets.toml`.
    """
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(tmp_path))
    (tmp_path / "targets.toml").write_text(
        'current_context = "missing"\n\n'
        '[contexts]\ndev = "http://ctx-dev:9000"\nprod = "https://ps.example.com"\n'
    )
    uncallable_client = _UnusedPsServiceClientMethods()

    exit_code = run(["config", "list-contexts"], client=uncallable_client)

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


def test_run_catalog_list_never_constructs_client_but_resolves_curated_repo_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`catalog list` reads `curated_repo_path` via `load_config()` but never touches
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

    exit_code = run(["catalog", "list"], client=uncallable_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "CRA-1.0  Cyber Resilience Act (external, EU)" in captured.out


class _FakeRestoreSuccessClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in whose restore_instrument() succeeds."""

    def restore_instrument(self, artifact: CuratedArtifact) -> RestorationResult:
        """Return a fixed RestorationResult, echoing the artifact's own instrument id."""
        return RestorationResult(
            instrument_id=artifact.manifest.instrument_id,
            stages=[RestorationStageOutcome(stage="verified", status="succeeded")],
        )


def test_run_catalog_restore_prints_instrument_id_on_mocked_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The full parser -> dispatch -> handler -> client wiring for `catalog restore`."""
    curated_repo_path = tmp_path / "curated-content"
    curated_repo_path.mkdir()
    _write_instrument_fixture(curated_repo_path, "CRA-1.0")
    monkeypatch.setenv("PS_CLI_CURATED_REPO_PATH", str(curated_repo_path))
    fake_client = _FakeRestoreSuccessClient()

    exit_code = run(["catalog", "restore", "CRA-1.0"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "instrument_id: CRA-1.0" in captured.out
    assert "verified: succeeded" in captured.out


def test_run_catalog_restore_missing_local_artifact_exits_one_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing local instrument directory surfaces as a clean PsCliError, exit 1."""
    curated_repo_path = tmp_path / "curated-content"
    curated_repo_path.mkdir()
    monkeypatch.setenv("PS_CLI_CURATED_REPO_PATH", str(curated_repo_path))
    uncallable_client = _UnusedPsServiceClientMethods()

    exit_code = run(["catalog", "restore", "MISSING-1.0"], client=uncallable_client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "curated instrument directory not found" in captured.err
    assert "Traceback" not in captured.err


# --- issue #68 Slice 10: `ps-cli health` end to end, through cli.run() -------------------


class _FakeHealthClient(_UnusedPsServiceClientMethods):
    """A duck-typed PsServiceClient stand-in with scripted check_health()/check_readiness().

    Mirrors `ps-cli/tests/modules/test_handlers.py::_FakeHealthClient` (Slices 8-9) at the
    `cli.run()` layer instead of calling `handle_health()` directly -- this file exercises
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


def test_run_health_returns_zero_on_happy_path(capsys: pytest.CaptureFixture[str]) -> None:
    """`run(["health"], client=<fully healthy fake>)` returns 0 and prints the three summary
    lines to stdout (AC-BI-004 end-to-end, D11).
    """
    fake_client = _FakeHealthClient(
        health_status="alive",
        readiness=ReadinessResult(status="ready", unhealthy_dependencies=[]),
    )

    exit_code = run(["health"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "reachable: yes" in captured.out
    assert "health: alive" in captured.out
    assert "ready: ready" in captured.out


def test_run_health_returns_one_with_distinct_message_when_not_ready(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`run(["health"], client=<not-ready fake>)` returns 1; stderr names the unhealthy
    dependency and is textually distinct from the unreachable-target wording (AC-BI-001,
    AC-BI-007, AC-BI-008 end-to-end).
    """
    fake_client = _FakeHealthClient(
        health_status="alive",
        readiness=ReadinessResult(status="not_ready", unhealthy_dependencies=["falkordb"]),
    )

    exit_code = run(["health"], client=fake_client)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "not ready" in captured.err
    assert "falkordb" in captured.err
    assert "Could not reach" not in captured.err


def test_run_health_with_unreachable_real_service_returns_one_without_crashing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-009's real-network proof for `health`, mirroring `test_run_with_unreachable_
    real_service_returns_one_without_crashing`'s exact pattern (PLAN.md §4 Slice 10):
    `run(["health"])` with `client=None` builds a real `PsServiceClient` from
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

    exit_code = run(["health"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Could not reach PS Service at" in captured.err
    assert "Traceback" not in captured.err


def test_run_health_with_context_flag_resolves_named_targets_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-BI-005/AC-BI-006 end-to-end for `health`, per CHANGES.md M2 (the only valid proof
    mechanism for this slice): an independent `load_config(context=..., config_dir=...)`
    check performed after `config set-context`/`config use-context` calls against an
    isolated `PS_CLI_CONFIG_DIR`, mirroring `test_ac_bi_005_set_context_then_use_context_
    drives_subsequent_resolution`'s exact pattern (issue #56 Slice 26).

    Deliberately does **not** call `run(["health", "--context", <name>], client=<fake>)`:
    `_resolve_client()` (`cli.py:42-55`) returns an injected `client` unchanged before
    `load_config(context=...)` is ever reached, so a fake client's presence would make such a
    test pass regardless of whether `--context` resolution actually worked -- it would prove
    nothing about `--context`. `_resolve_client()`'s own `load_config(context=context).
    service_url` call is exactly what this test proves resolves correctly; `health`'s
    `DISPATCH` entry reaches that same generic code path as every other client-backed
    command (D9), so this proof transfers to `health` without needing to invoke it at all.
    """
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
