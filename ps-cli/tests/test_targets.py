"""Tests for ps_cli.targets: resolve_config_dir(), TargetsFile, load_targets(), write_targets().

Slice 1: resolve_config_dir(). Slice 2: TargetsFile + load_targets() happy paths.
Slice 3: load_targets() malformed TOML -> PsCliError (AC-BI-010). Slice 13:
write_targets() round-trip. See PLAN.md (issue #56) §4 Slices 1-3, 13.

Issue #57 Slice 1 (D-57-2) rewrites `TargetsFile.contexts` from `dict[str, str]` to
`dict[str, ContextEntry]`, a nested-table `targets.toml` schema (`[contexts.<name>]`
+ `url = "..."`, optional `[contexts.<name>.auth]`) — see PLAN.md (issue #57) §2
Slice 1 and CHANGES.md F8.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ps_cli.errors import PsCliError
from ps_cli.targets import (
    AuthOverrides,
    ContextEntry,
    TargetsFile,
    load_targets,
    resolve_config_dir,
    write_targets,
)


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
    """A valid nested-table `targets.toml` (with `current_context` and two contexts) parses.

    Issue #57 Slice 1 (D-57-2): rewritten for the nested `[contexts.<name>]` shape --
    each context is a table with a `url` key, not a bare string.
    """
    (tmp_path / "targets.toml").write_text(
        'current_context = "dev"\n'
        "\n"
        "[contexts.dev]\n"
        'url = "http://127.0.0.1:8000"\n'
        "\n"
        "[contexts.prod]\n"
        'url = "https://ps.example.com"\n'
    )

    targets = load_targets(tmp_path)

    assert targets == TargetsFile(
        current_context="dev",
        contexts={
            "dev": ContextEntry(url="http://127.0.0.1:8000", auth=None),
            "prod": ContextEntry(url="https://ps.example.com", auth=None),
        },
    )


def test_load_targets_parses_context_with_auth_sub_table(tmp_path: Path) -> None:
    """A context with an `auth` sub-table parses into `ContextEntry.auth` as `AuthOverrides`."""
    (tmp_path / "targets.toml").write_text(
        "[contexts.dev]\n"
        'url = "http://127.0.0.1:8000"\n'
        "\n"
        "[contexts.dev.auth]\n"
        'issuer = "https://issuer.example"\n'
        'client_id = "cli-client"\n'
        'scopes = ["openid", "profile"]\n'
        'audience = "https://api.example"\n'
    )

    targets = load_targets(tmp_path)

    assert targets == TargetsFile(
        current_context=None,
        contexts={
            "dev": ContextEntry(
                url="http://127.0.0.1:8000",
                auth=AuthOverrides(
                    issuer="https://issuer.example",
                    client_id="cli-client",
                    scopes=("openid", "profile"),
                    audience="https://api.example",
                ),
            ),
        },
    )


def test_load_targets_parses_missing_current_context_as_none(tmp_path: Path) -> None:
    """A `targets.toml` with no `current_context` key parses to `current_context=None`."""
    (tmp_path / "targets.toml").write_text('[contexts.dev]\nurl = "http://127.0.0.1:8000"\n')

    targets = load_targets(tmp_path)

    assert targets == TargetsFile(
        current_context=None, contexts={"dev": ContextEntry(url="http://127.0.0.1:8000", auth=None)}
    )


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


def test_load_targets_with_pre_nested_flat_string_context_raises_ps_cli_error(
    tmp_path: Path,
) -> None:
    """A pre-#57 flat-string context value raises `PsCliError` naming the file, not a raw
    `AttributeError`/`TypeError`.

    Issue #57 Slice 1 (D-57-2): a `targets.toml` written before this issue's nested-table
    schema landed has `dev = "http://..."` directly under `[contexts]` -- a bare string,
    not a table. This must be caught explicitly and reported as a usability error pointing
    the operator at `config set-context` to rewrite the file, per PLAN.md (issue #57) §2
    Slice 1's "old targets.toml format" note.
    """
    targets_path = tmp_path / "targets.toml"
    targets_path.write_text('[contexts]\ndev = "http://127.0.0.1:8000"\n')

    with pytest.raises(PsCliError) as excinfo:
        load_targets(tmp_path)

    assert str(targets_path) in excinfo.value.msg


def test_write_targets_then_load_targets_round_trips(tmp_path: Path) -> None:
    """`write_targets()` followed by `load_targets()` returns an equal `TargetsFile`."""
    targets = TargetsFile(
        current_context="dev",
        contexts={
            "dev": ContextEntry(url="http://127.0.0.1:8000", auth=None),
            "prod": ContextEntry(url="https://ps.example.com", auth=None),
        },
    )

    write_targets(tmp_path, targets)

    assert load_targets(tmp_path) == targets


def test_write_targets_then_load_targets_round_trips_with_auth_overrides(
    tmp_path: Path,
) -> None:
    """A context's `auth` sub-table round-trips through `write_targets()`/`load_targets()`."""
    targets = TargetsFile(
        current_context=None,
        contexts={
            "dev": ContextEntry(
                url="http://127.0.0.1:8000",
                auth=AuthOverrides(
                    issuer="https://issuer.example",
                    client_id="cli-client",
                    scopes=("openid", "profile"),
                    audience="https://api.example",
                ),
            ),
            "prod": ContextEntry(url="https://ps.example.com", auth=None),
        },
    )

    write_targets(tmp_path, targets)

    assert load_targets(tmp_path) == targets


def test_write_targets_omits_none_auth_fields(tmp_path: Path) -> None:
    """An `AuthOverrides` with only some fields set omits the `None` ones entirely."""
    targets = TargetsFile(
        current_context=None,
        contexts={
            "dev": ContextEntry(
                url="http://127.0.0.1:8000",
                auth=AuthOverrides(
                    issuer="https://issuer.example", client_id=None, scopes=None, audience=None
                ),
            ),
        },
    )

    write_targets(tmp_path, targets)

    written_text = (tmp_path / "targets.toml").read_text(encoding="utf-8")
    assert "client_id" not in written_text
    assert "scopes" not in written_text
    assert "audience" not in written_text
    assert load_targets(tmp_path) == targets


def test_write_targets_creates_config_dir_if_missing(tmp_path: Path) -> None:
    """`write_targets()` creates `config_dir` (and parents) when it does not yet exist."""
    config_dir = tmp_path / "does" / "not" / "exist"
    targets = TargetsFile(
        current_context="dev",
        contexts={"dev": ContextEntry(url="http://127.0.0.1:8000", auth=None)},
    )

    write_targets(config_dir, targets)

    assert load_targets(config_dir) == targets


def test_write_targets_omits_current_context_line_when_none(tmp_path: Path) -> None:
    """`current_context=None` omits the line entirely, round-tripping back to `None`."""
    targets = TargetsFile(
        current_context=None, contexts={"dev": ContextEntry(url="http://127.0.0.1:8000", auth=None)}
    )

    write_targets(tmp_path, targets)

    written_text = (tmp_path / "targets.toml").read_text(encoding="utf-8")
    assert "current_context" not in written_text
    assert load_targets(tmp_path) == targets
