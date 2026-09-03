"""Tests for ps_cli.config: CliConfig resolution (packaged default / override / env var).

Increment 4: packaged default only. Increment 5: project-root override file merge.
Increment 6: env var precedence + URL validation. See PLAN.md §1 D3 / §3.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ps_cli.config import (
    CliConfig,
    _deep_merge,  # pyright: ignore[reportPrivateUsage]  # PLAN.md Increment 5: _deep_merge is unit-tested directly per its own AC
    is_valid_service_url,
    load_config,
)
from ps_cli.errors import PsCliError

if TYPE_CHECKING:
    from pathlib import Path


def test_load_config_with_no_override_file_and_no_env_var_returns_packaged_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `ps-cli.toml` in cwd, no `PS_CLI_SERVICE_URL` set -> the packaged default wins."""
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)

    config = load_config()

    assert config == CliConfig(service_url="http://127.0.0.1:8000")


def test_load_config_with_no_override_file_in_cwd_falls_through_to_packaged_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty `cwd` (no `ps-cli.toml` present) resolves via the packaged default."""
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)

    config = load_config(cwd=tmp_path)

    assert config == CliConfig(service_url="http://127.0.0.1:8000")


def test_load_config_with_override_file_in_cwd_uses_its_service_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `ps-cli.toml` in `cwd` with `service_url` set wins over the packaged default."""
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    (tmp_path / "ps-cli.toml").write_text('service_url = "http://example:9000"\n')

    config = load_config(cwd=tmp_path)

    assert config == CliConfig(service_url="http://example:9000")


def test_load_config_with_env_var_set_wins_over_override_file_and_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PS_CLI_SERVICE_URL` outranks both the override file and the packaged default."""
    (tmp_path / "ps-cli.toml").write_text('service_url = "http://example:9000"\n')
    monkeypatch.setenv("PS_CLI_SERVICE_URL", "https://env-wins.example:8443")

    config = load_config(cwd=tmp_path)

    assert config == CliConfig(service_url="https://env-wins.example:8443")


def test_load_config_with_empty_env_var_raises_ps_cli_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit but empty `PS_CLI_SERVICE_URL` fails closed rather than falling back."""
    monkeypatch.setenv("PS_CLI_SERVICE_URL", "")

    with pytest.raises(PsCliError) as excinfo:
        load_config(cwd=tmp_path)

    assert "PS_CLI_SERVICE_URL" in excinfo.value.msg
    assert "empty" in excinfo.value.msg


def test_load_config_with_malformed_env_var_url_raises_ps_cli_error_naming_env_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed `PS_CLI_SERVICE_URL` (no scheme/host) fails closed, naming the env var."""
    monkeypatch.setenv("PS_CLI_SERVICE_URL", "not-a-url")

    with pytest.raises(PsCliError) as excinfo:
        load_config(cwd=tmp_path)

    assert "not-a-url" in excinfo.value.msg
    assert "PS_CLI_SERVICE_URL" in (excinfo.value.hint or "")


def test_load_config_with_malformed_override_file_url_raises_ps_cli_error_naming_override_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed `service_url` in `ps-cli.toml` fails closed, naming the override file."""
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    (tmp_path / "ps-cli.toml").write_text('service_url = "not-a-url"\n')

    with pytest.raises(PsCliError) as excinfo:
        load_config(cwd=tmp_path)

    assert "not-a-url" in excinfo.value.msg
    assert "ps-cli.toml" in (excinfo.value.hint or "")


def test_deep_merge_recursively_merges_nested_dicts_with_override_winning() -> None:
    """`_deep_merge` merges nested dicts key-by-key; override wins on scalar conflicts.

    Exercised directly with a nested-dict fixture, even though today's real
    config schema (`service_url`) is flat — proves the "deep" part of the
    merge per PLAN.md D3's wording ahead of the schema growing nested tables.
    """
    base: dict[str, object] = {
        "service_url": "http://127.0.0.1:8000",
        "retry": {"attempts": 3, "backoff": {"initial_seconds": 1}},
    }
    override: dict[str, object] = {
        "retry": {"backoff": {"initial_seconds": 5}, "max_seconds": 30},
    }

    merged = _deep_merge(base, override)

    assert merged == {
        "service_url": "http://127.0.0.1:8000",
        "retry": {
            "attempts": 3,
            "backoff": {"initial_seconds": 5},
            "max_seconds": 30,
        },
    }


def test_load_config_with_config_dir_but_no_targets_toml_falls_through_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `config_dir` with no `targets.toml` in it falls through to today's resolver, unchanged.

    AC-BI-004 regression proof (PLAN.md Slice 5, critical): `context`/`config_dir` are new
    keyword-only parameters (PLAN.md D3) with a no-op-for-now body when no `targets.toml`
    exists — `load_config()` resolves exactly as it does today. See CHANGES.md F2 for why
    this holds hermetically (an autouse fixture in `ps-cli/tests/conftest.py` isolates
    `PS_CLI_CONFIG_DIR` for every test).
    """
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    empty_config_dir = tmp_path / "config"

    config = load_config(cwd=tmp_path, config_dir=empty_config_dir)

    assert config == CliConfig(service_url="http://127.0.0.1:8000")


def test_is_valid_service_url_accepts_http_and_https_with_hostname() -> None:
    """A valid http(s) URL with a non-empty hostname is accepted (PLAN.md D5)."""
    assert is_valid_service_url("http://127.0.0.1:8000") is True
    assert is_valid_service_url("https://ps.example.com") is True


def test_is_valid_service_url_rejects_missing_scheme_or_hostname() -> None:
    """A URL missing a valid scheme and/or a hostname is rejected (PLAN.md D5)."""
    assert is_valid_service_url("not-a-url") is False
    assert is_valid_service_url("http://") is False
    assert is_valid_service_url("ftp://example.com") is False


def test_load_config_resolves_url_from_current_context_when_no_env_var_or_context_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`current_context` in `targets.toml` resolves the URL (AC-BI-001, PLAN.md Slice 6)."""
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "targets.toml").write_text(
        'current_context = "dev"\n\n[contexts]\ndev = "http://ctx-dev:9000"\n'
    )

    config = load_config(cwd=tmp_path, config_dir=config_dir)

    assert config == CliConfig(service_url="http://ctx-dev:9000")


def test_load_config_env_var_overrides_context_param_and_current_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PS_CLI_SERVICE_URL` outranks both `context` and `current_context` (AC-BI-002, Slice 7)."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "targets.toml").write_text(
        'current_context = "dev"\n\n[contexts]\n'
        'dev = "http://ctx-dev:9000"\n'
        'prod = "https://ctx-prod.example"\n'
    )
    monkeypatch.setenv("PS_CLI_SERVICE_URL", "https://env-wins.example")

    config = load_config(cwd=tmp_path, context="prod", config_dir=config_dir)

    assert config == CliConfig(service_url="https://env-wins.example")


def test_load_config_raises_on_invalid_context_param_even_when_env_var_would_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid `--context` name always errors, even though the env var would otherwise win.

    CHANGES.md F5 (inserted Slice 7.5): AC-BI-009 is unconditional — the env var's
    presence must not suppress validation of an explicitly-passed `context` param.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "targets.toml").write_text(
        '[contexts]\ndev = "http://ctx-dev:9000"\nprod = "https://ctx-prod.example"\n'
    )
    monkeypatch.setenv("PS_CLI_SERVICE_URL", "https://env-wins.example")

    with pytest.raises(PsCliError) as excinfo:
        load_config(cwd=tmp_path, context="qa", config_dir=config_dir)

    assert "qa" in excinfo.value.msg
    combined = f"{excinfo.value.msg} {excinfo.value.hint or ''}"
    assert "dev" in combined
    assert "prod" in combined


def test_load_config_context_param_overrides_current_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`context` param overrides `current_context` (AC-BI-006 config-layer half, Slice 8)."""
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "targets.toml").write_text(
        'current_context = "dev"\n\n[contexts]\n'
        'dev = "http://ctx-dev:9000"\n'
        'prod = "https://ctx-prod.example"\n'
    )

    config = load_config(cwd=tmp_path, context="prod", config_dir=config_dir)

    assert config == CliConfig(service_url="https://ctx-prod.example")


def test_load_config_raises_when_current_context_names_missing_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`current_context` naming a missing context raises `PsCliError` (AC-BI-008, Slice 9)."""
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "targets.toml").write_text(
        'current_context = "staging"\n\n[contexts]\n'
        'dev = "http://ctx-dev:9000"\n'
        'prod = "https://ctx-prod.example"\n'
    )

    with pytest.raises(PsCliError) as excinfo:
        load_config(cwd=tmp_path, config_dir=config_dir)

    assert "staging" in excinfo.value.msg


def test_load_config_raises_when_context_param_names_missing_context_lists_valid_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`context` param naming a missing context raises, listing valid names.

    AC-BI-009 (PLAN.md Slice 10).
    """
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "targets.toml").write_text(
        '[contexts]\ndev = "http://ctx-dev:9000"\nprod = "https://ctx-prod.example"\n'
    )

    with pytest.raises(PsCliError) as excinfo:
        load_config(cwd=tmp_path, context="qa", config_dir=config_dir)

    assert "qa" in excinfo.value.msg
    combined = f"{excinfo.value.msg} {excinfo.value.hint or ''}"
    assert "dev" in combined
    assert "prod" in combined


def test_load_config_config_dir_param_selects_which_targets_toml_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`config_dir` (not a hardcoded default) drives which `targets.toml` is read.

    AC-BI-003, targets half (PLAN.md Slice 11). Two separate `config_dir`s, each
    with its own `targets.toml` defining a different `current_context`, resolve to
    each dir's own value.
    """
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    config_dir_a = tmp_path / "config-a"
    config_dir_a.mkdir()
    (config_dir_a / "targets.toml").write_text(
        'current_context = "dev"\n\n[contexts]\ndev = "http://ctx-dev-a:9000"\n'
    )
    config_dir_b = tmp_path / "config-b"
    config_dir_b.mkdir()
    (config_dir_b / "targets.toml").write_text(
        'current_context = "dev"\n\n[contexts]\ndev = "http://ctx-dev-b:9000"\n'
    )

    config_a = load_config(cwd=tmp_path, config_dir=config_dir_a)
    config_b = load_config(cwd=tmp_path, config_dir=config_dir_b)

    assert config_a == CliConfig(service_url="http://ctx-dev-a:9000")
    assert config_b == CliConfig(service_url="http://ctx-dev-b:9000")


def test_load_config_uses_ps_cli_config_dir_env_var_when_config_dir_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting `config_dir` falls back to `resolve_config_dir()`, honoring `PS_CLI_CONFIG_DIR`.

    AC-BI-003, targets half (PLAN.md Slice 11): proves the env-var plumbing, not just
    the `config_dir=...` constructor-injection seam exercised by the sibling test above.
    """
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    env_config_dir = tmp_path / "env-config"
    env_config_dir.mkdir()
    (env_config_dir / "targets.toml").write_text(
        'current_context = "dev"\n\n[contexts]\ndev = "http://ctx-dev-env:9000"\n'
    )
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(env_config_dir))

    config = load_config(cwd=tmp_path)

    assert config == CliConfig(service_url="http://ctx-dev-env:9000")
