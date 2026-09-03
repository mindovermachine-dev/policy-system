"""Tests for ps_cli.targets: resolve_config_dir(), TargetsFile, load_targets(), write_targets().

Slice 1: resolve_config_dir(). Slice 2: TargetsFile + load_targets() happy paths.
Slice 3: load_targets() malformed TOML -> PsCliError (AC-BI-010). Slice 13:
write_targets() round-trip. See PLAN.md (issue #56) §4 Slices 1-3, 13.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ps_cli.errors import PsCliError
from ps_cli.targets import TargetsFile, load_targets, resolve_config_dir, write_targets


def test_resolve_config_dir_uses_env_var_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PS_CLI_CONFIG_DIR`, when set, wins outright over the default."""
    override_dir = tmp_path / "x"
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(override_dir))

    assert resolve_config_dir() == override_dir


def test_resolve_config_dir_defaults_to_home_dot_config_ps_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no override, the default is `~/.config/ps-cli/`."""
    monkeypatch.delenv("PS_CLI_CONFIG_DIR", raising=False)

    assert resolve_config_dir() == Path.home() / ".config" / "ps-cli"


def test_load_targets_returns_none_when_file_absent(tmp_path: Path) -> None:
    """No `targets.toml` in `config_dir` -> `load_targets()` returns `None`."""
    assert load_targets(tmp_path) is None


def test_load_targets_parses_valid_file(tmp_path: Path) -> None:
    """A valid `targets.toml` (with `current_context` and two contexts) parses correctly."""
    (tmp_path / "targets.toml").write_text(
        'current_context = "dev"\n'
        "\n"
        "[contexts]\n"
        'dev = "http://127.0.0.1:8000"\n'
        'prod = "https://ps.example.com"\n'
    )

    targets = load_targets(tmp_path)

    assert targets == TargetsFile(
        current_context="dev",
        contexts={"dev": "http://127.0.0.1:8000", "prod": "https://ps.example.com"},
    )


def test_load_targets_parses_missing_current_context_as_none(tmp_path: Path) -> None:
    """A `targets.toml` with no `current_context` key parses to `current_context=None`."""
    (tmp_path / "targets.toml").write_text('[contexts]\ndev = "http://127.0.0.1:8000"\n')

    targets = load_targets(tmp_path)

    assert targets == TargetsFile(current_context=None, contexts={"dev": "http://127.0.0.1:8000"})


def test_load_targets_with_malformed_toml_raises_ps_cli_error_naming_file_path(
    tmp_path: Path,
) -> None:
    """Malformed TOML in `targets.toml` raises `PsCliError` naming the file path.

    AC-BI-010. Deliberate divergence from `ps-cli.toml`'s unwrapped-crash behavior
    for malformed TOML today — see PLAN.md (issue #56) §1 D4.
    """
    targets_path = tmp_path / "targets.toml"
    targets_path.write_text("not valid toml {{{")

    with pytest.raises(PsCliError) as excinfo:
        load_targets(tmp_path)

    assert str(targets_path) in excinfo.value.msg


def test_write_targets_then_load_targets_round_trips(tmp_path: Path) -> None:
    """`write_targets()` followed by `load_targets()` returns an equal `TargetsFile`."""
    targets = TargetsFile(
        current_context="dev",
        contexts={"dev": "http://127.0.0.1:8000", "prod": "https://ps.example.com"},
    )

    write_targets(tmp_path, targets)

    assert load_targets(tmp_path) == targets


def test_write_targets_creates_config_dir_if_missing(tmp_path: Path) -> None:
    """`write_targets()` creates `config_dir` (and parents) when it does not yet exist."""
    config_dir = tmp_path / "does" / "not" / "exist"
    targets = TargetsFile(current_context="dev", contexts={"dev": "http://127.0.0.1:8000"})

    write_targets(config_dir, targets)

    assert load_targets(config_dir) == targets


def test_write_targets_omits_current_context_line_when_none(tmp_path: Path) -> None:
    """`current_context=None` omits the line entirely, round-tripping back to `None`."""
    targets = TargetsFile(current_context=None, contexts={"dev": "http://127.0.0.1:8000"})

    write_targets(tmp_path, targets)

    written_text = (tmp_path / "targets.toml").read_text(encoding="utf-8")
    assert "current_context" not in written_text
    assert load_targets(tmp_path) == targets
