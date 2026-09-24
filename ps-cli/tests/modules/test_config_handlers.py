"""Tests for ps_cli.modules.config_handlers: `handle_config_set_context()` (issue #56).

Slice 22: writes `targets.toml`, never a credential (AC-BI-012, PLAN.md §4 Slice 22).
Slice 23: `delete_tokens` on every re-run, unconditionally (AC-BI-014, PLAN.md §4
Slice 23, D13).
Slice 25: `handle_config_use_context()` -- success + unknown-name error (AC-BI-005 half,
AC-BI-009 command half, PLAN.md §4 Slice 25).

Issue #57 Slice 1 (D-57-2) rewrites `targets.toml`'s `[contexts]` shape from flat
`name = "url"` strings to nested `[contexts.<name>]` tables with a `url` key; every
fixture and assertion below that touched the old flat shape is updated to the new one.

Issue #121: `build_credential_store()` becomes zero-argument (D-121-5) -- the old
`PS_CLI_CONFIG_DIR`-drives-`credentials.toml`-resolution proof this file used to carry
(CHANGES.md issue #56 F7) no longer applies and is deleted (see the note where it used
to live, just above the Slice 25 section).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ps_cli.errors import PsCliError
from ps_cli.modules.config_handlers import (
    handle_config_get_contexts,
    handle_config_set_context,
    handle_config_use_context,
)
from ps_cli.targets import AuthOverrides, ContextEntry, TargetsFile, load_targets, write_targets

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import InMemoryKeyringBackend

    from ps_cli.credentials import TokenBundle


def test_handle_config_set_context_writes_url_creates_new_entry(
    tmp_path: Path, portable_keyring: InMemoryKeyringBackend
) -> None:
    """A brand-new context name is written into `targets.toml`'s `[contexts]` table."""
    del portable_keyring  # only needed so build_credential_store() has a portable backend
    handle_config_set_context("prod", "https://ps.example.com", config_dir=tmp_path)

    targets = load_targets(tmp_path)

    assert targets is not None
    assert targets.contexts == {"prod": ContextEntry(url="https://ps.example.com", auth=None)}


# A set of fake-credential marker strings used elsewhere in this test suite
# (`tests/test_credentials.py`) -- a literal grep-style check that none of them ever land
# in `targets.toml`'s raw content, structurally proving AC-BI-012 rather than merely
# trusting `TargetsFile`'s schema shape.
_CREDENTIAL_MARKER_STRINGS = (
    "super-secret-token-value-should-never-print",
    "seeded-tok",
    "tok-1",
)


def test_handle_config_set_context_file_content_never_contains_a_credential_value(
    tmp_path: Path,
    portable_keyring: InMemoryKeyringBackend,
) -> None:
    """`targets.toml`'s raw file content has the URL, never a credential marker (AC-BI-012)."""
    del portable_keyring  # only needed so build_credential_store() has a portable backend
    handle_config_set_context("prod", "https://ps.example.com", config_dir=tmp_path)

    raw_content = (tmp_path / "targets.toml").read_text(encoding="utf-8")

    assert "https://ps.example.com" in raw_content
    for marker in _CREDENTIAL_MARKER_STRINGS:
        assert marker not in raw_content


class _RecordingCredentialStore:
    """A `CredentialStore` spy recording every `delete_tokens` call, in call order."""

    def __init__(self) -> None:
        """Initialize with no recorded deletions yet."""
        self.deleted: list[str] = []

    def get_tokens(self, context: str) -> TokenBundle | None:
        """Unused by this spy's tests; return `None` unconditionally."""
        del context
        return None

    def set_tokens(self, context: str, tokens: TokenBundle) -> None:
        """Unused by this spy's tests; no-op."""
        del context, tokens

    def delete_tokens(self, context: str) -> None:
        """Record `context`, in call order -- never raises."""
        self.deleted.append(context)


def test_handle_config_set_context_deletes_credential_unconditionally_on_every_call(
    tmp_path: Path,
) -> None:
    """`delete_tokens` fires on every `set-context` call, including the first (AC-BI-014).

    D13: deletion is unconditional, not "only when the name already existed" -- a recording
    spy proves both the brand-new-context call and the re-run call each trigger a delete.
    """
    spy = _RecordingCredentialStore()

    handle_config_set_context("prod", "https://a", config_dir=tmp_path, credential_store=spy)
    handle_config_set_context("prod", "https://b", config_dir=tmp_path, credential_store=spy)

    assert spy.deleted == ["prod", "prod"]


# Issue #121: the old `test_handle_config_set_context_credential_delete_resolves_under_
# ps_cli_config_dir_env_var` test (proving `PS_CLI_CONFIG_DIR`-driven isolation across two
# different `config_dir`s) is deleted here, not salvaged -- `build_credential_store()` is
# now zero-argument (D-121-5), so `config_dir` plays no role in credential resolution at
# all any more. What that test's premise actually cared about -- two different contexts
# never colliding -- is already covered by `test_credentials.py`'s
# `test_keyring_credential_store_isolates_credentials_per_context_name`, which isolates by
# context *name* within one `KeyringCredentialStore`, the only isolation axis left.


# --- issue #56 Slice 25: handle_config_use_context() -----------------------------------


def test_handle_config_set_context_writes_auth_issuer_override(
    tmp_path: Path, portable_keyring: InMemoryKeyringBackend
) -> None:
    """`auth_issuer="https://issuer.example"` writes `ContextEntry.auth.issuer` (AC-BI-006)."""
    del portable_keyring  # only needed so build_credential_store() has a portable backend
    handle_config_set_context(
        "prod",
        "https://ps.example.com",
        config_dir=tmp_path,
        auth_issuer="https://issuer.example",
    )

    targets = load_targets(tmp_path)

    assert targets is not None
    assert targets.contexts["prod"].auth == AuthOverrides(
        issuer="https://issuer.example", client_id=None, scopes=None, audience=None
    )


def test_handle_config_set_context_preserves_existing_auth_when_only_url_flag_given_again(
    tmp_path: Path,
    portable_keyring: InMemoryKeyringBackend,
) -> None:
    """Re-running `set-context` with only `--url` (no `--auth-*` flags) leaves the
    context's existing `auth` table untouched (AC-BI-006: omitted flags never clear).
    """
    del portable_keyring  # only needed so build_credential_store() has a portable backend
    handle_config_set_context(
        "prod",
        "https://ps.example.com",
        config_dir=tmp_path,
        auth_issuer="https://issuer.example",
        auth_client_id="cli-client-id",
    )

    handle_config_set_context("prod", "https://ps.example.com/v2", config_dir=tmp_path)

    targets = load_targets(tmp_path)
    assert targets is not None
    assert targets.contexts["prod"].url == "https://ps.example.com/v2"
    assert targets.contexts["prod"].auth == AuthOverrides(
        issuer="https://issuer.example", client_id="cli-client-id", scopes=None, audience=None
    )


def test_handle_config_set_context_overwrites_only_the_passed_auth_field_leaving_others_intact(
    tmp_path: Path,
    portable_keyring: InMemoryKeyringBackend,
) -> None:
    """Passing only `--auth-audience` on a re-run overwrites just that field, leaving
    `issuer`/`client_id`/`scopes` exactly as they were (AC-BI-006's per-field wording).
    """
    del portable_keyring  # only needed so build_credential_store() has a portable backend
    handle_config_set_context(
        "prod",
        "https://ps.example.com",
        config_dir=tmp_path,
        auth_issuer="https://issuer.example",
        auth_client_id="cli-client-id",
        auth_scopes=("openid", "profile"),
    )

    handle_config_set_context(
        "prod", "https://ps.example.com", config_dir=tmp_path, auth_audience="ps-service"
    )

    targets = load_targets(tmp_path)
    assert targets is not None
    assert targets.contexts["prod"].auth == AuthOverrides(
        issuer="https://issuer.example",
        client_id="cli-client-id",
        scopes=("openid", "profile"),
        audience="ps-service",
    )


def test_handle_config_set_context_no_auth_flags_and_no_existing_auth_stays_none(
    tmp_path: Path,
    portable_keyring: InMemoryKeyringBackend,
) -> None:
    """A brand-new context with no `--auth-*` flags gets `auth=None`, not an empty table."""
    del portable_keyring  # only needed so build_credential_store() has a portable backend
    handle_config_set_context("prod", "https://ps.example.com", config_dir=tmp_path)

    targets = load_targets(tmp_path)
    assert targets is not None
    assert targets.contexts["prod"].auth is None


def test_handle_config_use_context_sets_current_context(
    tmp_path: Path, portable_keyring: InMemoryKeyringBackend
) -> None:
    """`use-context prod` sets `current_context` to `prod`, `[contexts]` unchanged."""
    del portable_keyring  # only needed so build_credential_store() has a portable backend
    handle_config_set_context("dev", "http://ctx-dev:9000", config_dir=tmp_path)
    handle_config_set_context("prod", "https://ps.example.com", config_dir=tmp_path)

    handle_config_use_context("prod", config_dir=tmp_path)

    targets = load_targets(tmp_path)
    assert targets is not None
    assert targets.current_context == "prod"
    assert targets.contexts == {
        "dev": ContextEntry(url="http://ctx-dev:9000", auth=None),
        "prod": ContextEntry(url="https://ps.example.com", auth=None),
    }


def test_handle_config_use_context_raises_listing_valid_names_for_unknown_context(
    tmp_path: Path,
    portable_keyring: InMemoryKeyringBackend,
) -> None:
    """`use-context qa` with only `dev`/`prod` defined raises, listing the valid names."""
    del portable_keyring  # only needed so build_credential_store() has a portable backend
    handle_config_set_context("dev", "http://ctx-dev:9000", config_dir=tmp_path)
    handle_config_set_context("prod", "https://ps.example.com", config_dir=tmp_path)

    with pytest.raises(PsCliError) as excinfo:
        handle_config_use_context("qa", config_dir=tmp_path)

    combined = f"{excinfo.value.msg} {excinfo.value.hint or ''}"
    assert "dev" in combined
    assert "prod" in combined


# --- issue #56 Slice 28: handle_config_get_contexts() ----------------------------------


def test_handle_config_get_contexts_marks_current_context(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    portable_keyring: InMemoryKeyringBackend,
) -> None:
    """Two contexts, one current -- both appear in stdout as a bordered table; exactly one
    line carries `*`, the current context's line (AC-BI-005).
    """
    del portable_keyring  # only needed so build_credential_store() has a portable backend
    handle_config_set_context("dev", "http://ctx-dev:9000", config_dir=tmp_path)
    handle_config_set_context("prod", "https://ps.example.com", config_dir=tmp_path)
    handle_config_use_context("prod", config_dir=tmp_path)

    handle_config_get_contexts(config_dir=tmp_path)

    out = capsys.readouterr().out
    assert "Current" in out
    assert "Name" in out
    assert "URL" in out
    assert "dev" in out
    assert "http://ctx-dev:9000" in out
    assert "prod" in out
    assert "https://ps.example.com" in out
    starred_lines = [line for line in out.splitlines() if "*" in line]
    assert len(starred_lines) == 1
    assert "prod" in starred_lines[0]


def test_handle_config_get_contexts_does_not_truncate_a_wide_url(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    portable_keyring: InMemoryKeyringBackend,
) -> None:
    """AC-BI-006, handler level: a 120-char context URL is never truncated."""
    del portable_keyring  # only needed so build_credential_store() has a portable backend
    wide_url = "https://" + "a" * 112
    assert len(wide_url) == 120
    handle_config_set_context("dev", wide_url, config_dir=tmp_path)

    handle_config_get_contexts(config_dir=tmp_path)

    assert wide_url in capsys.readouterr().out


def test_handle_config_get_contexts_renders_a_hostile_url_literally(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    r"""AC-BI-009, handler level: a hand-edited `targets.toml` carrying a control-sequence
    URL renders literally, no crash.

    `write_targets()`'s escaper (`toml_writer.escape_basic_string`) only escapes
    backslash/quote/tab/newline/CR, not arbitrary control bytes -- a raw ESC byte cannot
    round-trip through `write_targets()`/`handle_config_set_context()` at all (`tomllib`
    rejects an unescaped control byte in a TOML string as invalid TOML; confirmed
    empirically). The real threat model here is an operator hand-editing `targets.toml`
    with a properly TOML-escaped control sequence (`\\u001b`, valid TOML, which `tomllib`
    decodes back to a real ESC byte) -- so this test writes the file directly rather than
    going through `handle_config_set_context()`.
    """
    (tmp_path / "targets.toml").write_text(
        '[contexts.dev]\nurl = "[bold]Injected[/bold] \\u001b[31mFakeAnsi\\u001b[0m"\n',
        encoding="utf-8",
    )

    handle_config_get_contexts(config_dir=tmp_path)

    out = capsys.readouterr().out
    assert "[bold]Injected[/bold]" in out
    assert "\x1b" not in out


def test_handle_config_get_contexts_shows_auth_overrides_column(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    portable_keyring: InMemoryKeyringBackend,
) -> None:
    """The "Auth Overrides" column lists field *names* only, never their values
    (issue #57 Slice 8): "-" for a context with no overrides, "issuer, audience" for
    one with those two fields set, and the literal override values themselves never
    appear anywhere in stdout.
    """
    del portable_keyring  # only needed so build_credential_store() has a portable backend
    handle_config_set_context("dev", "http://ctx-dev:9000", config_dir=tmp_path)
    handle_config_set_context(
        "prod",
        "https://ps.example.com",
        config_dir=tmp_path,
        auth_issuer="https://issuer.example",
        auth_audience="ps-service",
    )

    handle_config_get_contexts(config_dir=tmp_path)

    out = capsys.readouterr().out
    assert "Auth Overrides" in out
    lines = out.splitlines()
    dev_line = next(line for line in lines if "dev" in line)
    prod_line = next(line for line in lines if "prod" in line)
    assert "-" in dev_line
    assert "issuer, audience" in prod_line
    assert "https://issuer.example" not in out
    assert "ps-service" not in out


def test_handle_config_get_contexts_with_no_targets_toml_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No `targets.toml` -> empty stdout; not an error state (AC-BI-007)."""
    handle_config_get_contexts(config_dir=tmp_path)

    assert capsys.readouterr().out == ""


def test_handle_config_get_contexts_with_empty_contexts_table_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-008 (empty-`[contexts]` sub-case): a targets.toml with no contexts prints nothing."""
    write_targets(tmp_path, TargetsFile(current_context=None, contexts={}))

    handle_config_get_contexts(config_dir=tmp_path)

    assert capsys.readouterr().out == ""
