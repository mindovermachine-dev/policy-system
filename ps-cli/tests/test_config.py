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
