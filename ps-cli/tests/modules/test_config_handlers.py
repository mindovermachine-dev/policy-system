"""Tests for ps_cli.modules.config_handlers: `handle_config_set_context()` (issue #56).

Slice 22: writes `targets.toml`, never a credential (AC-BI-012, PLAN.md §4 Slice 22).
Slice 23: `delete_credential` on every re-run, unconditionally (AC-BI-014, PLAN.md §4
Slice 23, D13). Slice 23.5 (CHANGES.md F7, not in PLAN.md): end-to-end proof that
`PS_CLI_CONFIG_DIR` also drives `credentials.toml` resolution when both `config_dir` and
`credential_store` are omitted, mirroring Slice 11's `targets.toml`-half proof.
Slice 25: `handle_config_use_context()` -- success + unknown-name error (AC-BI-005 half,
AC-BI-009 command half, PLAN.md §4 Slice 25).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ps_cli.credentials import FileCredentialStore
from ps_cli.errors import PsCliError
from ps_cli.modules.config_handlers import (
    handle_config_list_contexts,
    handle_config_set_context,
    handle_config_use_context,
)
from ps_cli.targets import load_targets

if TYPE_CHECKING:
    from pathlib import Path


def test_handle_config_set_context_writes_url_creates_new_entry(tmp_path: Path) -> None:
    """A brand-new context name is written into `targets.toml`'s `[contexts]` table."""
    handle_config_set_context("prod", "https://ps.example.com", config_dir=tmp_path)

    targets = load_targets(tmp_path)

    assert targets is not None
    assert targets.contexts == {"prod": "https://ps.example.com"}


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
) -> None:
    """`targets.toml`'s raw file content has the URL, never a credential marker (AC-BI-012)."""
    handle_config_set_context("prod", "https://ps.example.com", config_dir=tmp_path)

    raw_content = (tmp_path / "targets.toml").read_text(encoding="utf-8")

    assert "https://ps.example.com" in raw_content
    for marker in _CREDENTIAL_MARKER_STRINGS:
        assert marker not in raw_content


class _RecordingCredentialStore:
    """A `CredentialStore` spy recording every `delete_credential` call, in call order."""

    def __init__(self) -> None:
        """Initialize with no recorded deletions yet."""
        self.deleted: list[str] = []

    def get_credential(self, context: str) -> str | None:
        """Unused by this spy's tests; return `None` unconditionally."""
        del context
        return None

    def set_credential(self, context: str, credential: str) -> None:
        """Unused by this spy's tests; no-op."""
        del context, credential

    def delete_credential(self, context: str) -> None:
        """Record `context`, in call order -- never raises."""
        self.deleted.append(context)


def test_handle_config_set_context_deletes_credential_unconditionally_on_every_call(
    tmp_path: Path,
) -> None:
    """`delete_credential` fires on every `set-context` call, including the first (AC-BI-014).

    D13: deletion is unconditional, not "only when the name already existed" -- a recording
    spy proves both the brand-new-context call and the re-run call each trigger a delete.
    """
    spy = _RecordingCredentialStore()

    handle_config_set_context("prod", "https://a", config_dir=tmp_path, credential_store=spy)
    handle_config_set_context("prod", "https://b", config_dir=tmp_path, credential_store=spy)

    assert spy.deleted == ["prod", "prod"]


def test_handle_config_set_context_credential_delete_resolves_under_ps_cli_config_dir_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PS_CLI_CONFIG_DIR` drives `credentials.toml` resolution too (CHANGES.md F7).

    `build_credential_store(config_dir: Path)` takes a required, non-defaulted `config_dir`
    -- so this proof cannot live inside `build_credential_store()` itself; it must exercise
    the real call site (`handle_config_set_context`, with both `config_dir` and
    `credential_store` omitted) that lets `config_dir` default via `resolve_config_dir()`,
    exactly like a real CLI invocation. A credential is pre-seeded directly via
    `FileCredentialStore`, bypassing keyring entirely. A second, sibling `tmp_path`-adjacent
    dir with its own seeded credential, asserted untouched afterward, rules out a false
    positive from a shared/global fallback path.
    """
    primary_dir = tmp_path / "primary"
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(primary_dir))
    FileCredentialStore(primary_dir).set_credential("dev", "seed-token")

    sibling_dir = tmp_path / "sibling"
    FileCredentialStore(sibling_dir).set_credential("dev", "sibling-token")

    handle_config_set_context(
        "dev", "https://ps.example.com", config_dir=None, credential_store=None
    )

    assert FileCredentialStore(primary_dir).get_credential("dev") is None
    assert FileCredentialStore(sibling_dir).get_credential("dev") == "sibling-token"


# --- issue #56 Slice 25: handle_config_use_context() -----------------------------------


def test_handle_config_use_context_sets_current_context(tmp_path: Path) -> None:
    """`use-context prod` sets `current_context` to `prod`, `[contexts]` unchanged."""
    handle_config_set_context("dev", "http://ctx-dev:9000", config_dir=tmp_path)
    handle_config_set_context("prod", "https://ps.example.com", config_dir=tmp_path)

    handle_config_use_context("prod", config_dir=tmp_path)

    targets = load_targets(tmp_path)
    assert targets is not None
    assert targets.current_context == "prod"
    assert targets.contexts == {"dev": "http://ctx-dev:9000", "prod": "https://ps.example.com"}


def test_handle_config_use_context_raises_listing_valid_names_for_unknown_context(
    tmp_path: Path,
) -> None:
    """`use-context qa` with only `dev`/`prod` defined raises, listing the valid names."""
    handle_config_set_context("dev", "http://ctx-dev:9000", config_dir=tmp_path)
    handle_config_set_context("prod", "https://ps.example.com", config_dir=tmp_path)

    with pytest.raises(PsCliError) as excinfo:
        handle_config_use_context("qa", config_dir=tmp_path)

    combined = f"{excinfo.value.msg} {excinfo.value.hint or ''}"
    assert "dev" in combined
    assert "prod" in combined


# --- issue #56 Slice 28: handle_config_list_contexts() ----------------------------------


def test_handle_config_list_contexts_marks_current_context(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two contexts, one current -- both appear in stdout; exactly one line starts with `*`,
    the current context's line (AC-BI-007).
    """
    handle_config_set_context("dev", "http://ctx-dev:9000", config_dir=tmp_path)
    handle_config_set_context("prod", "https://ps.example.com", config_dir=tmp_path)
    handle_config_use_context("prod", config_dir=tmp_path)

    handle_config_list_contexts(config_dir=tmp_path)

    out_lines = capsys.readouterr().out.splitlines()
    assert "dev" in "\n".join(out_lines)
    assert "http://ctx-dev:9000" in "\n".join(out_lines)
    assert "prod" in "\n".join(out_lines)
    assert "https://ps.example.com" in "\n".join(out_lines)
    starred = [line for line in out_lines if line.startswith("*")]
    assert len(starred) == 1
    assert "prod" in starred[0]


def test_handle_config_list_contexts_with_no_targets_toml_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No `targets.toml` -> empty stdout; not an error state (AC-BI-007)."""
    handle_config_list_contexts(config_dir=tmp_path)

    assert capsys.readouterr().out == ""
