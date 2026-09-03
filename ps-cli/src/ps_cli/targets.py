"""ps-cli target/context configuration: `targets.toml` (named contexts) resolution.

New module introduced by issue #56 (multi-target config model). `targets.toml` maps
named contexts (e.g. "dev", "prod") to PS Service URLs and records which context is
currently selected (`current_context`). See PLAN.md (issue #56) §1 D1 for the file
schema, D2 for config-directory resolution, and D4 for why a malformed `targets.toml`
raises `PsCliError` (unlike `ps-cli.toml`'s unwrapped-crash behavior today).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ps_cli.errors import PsCliError
from ps_cli.toml_writer import escape_basic_string, format_flat_table

_TARGETS_FILE_NAME = "targets.toml"


def resolve_config_dir() -> Path:
    """Resolve ps-cli's config directory.

    `PS_CLI_CONFIG_DIR`, when set, wins outright; otherwise defaults to
    `~/.config/ps-cli/`, mirroring `gh`'s `GH_CONFIG_DIR` convention. See
    PLAN.md (issue #56) §1 D2.
    """
    override = os.environ.get("PS_CLI_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "ps-cli"


@dataclass(frozen=True)
class TargetsFile:
    """Parsed contents of `targets.toml`: named contexts plus the currently-selected one.

    `current_context` is `None` when the key is absent from the file (no context has
    been selected yet) — distinct from an explicitly-empty string. See PLAN.md
    (issue #56) §1 D1.
    """

    current_context: str | None
    contexts: dict[str, str]


def load_targets(config_dir: Path) -> TargetsFile | None:
    """Load and parse `<config_dir>/targets.toml`.

    Returns `None` if the file does not exist — `targets.toml` is entirely optional;
    a caller falls through to the legacy resolver in that case (see PLAN.md
    (issue #56) §1 D3, case 4). Raises `PsCliError` naming the file path if the file
    exists but contains invalid TOML (AC-BI-010) — a deliberate divergence from
    `ps-cli.toml`'s unwrapped-crash behavior for malformed TOML today: `targets.toml`
    is operator-hand-edited, so a parse failure here is a user-facing error, not a
    packaging/environment bug. See PLAN.md (issue #56) §1 D4.
    """
    targets_path = config_dir / _TARGETS_FILE_NAME
    if not targets_path.is_file():
        return None

    try:
        raw = tomllib.loads(targets_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PsCliError(msg=f"{targets_path} contains invalid TOML: {exc}") from exc

    # `tomllib.loads` returns `dict[str, Any]`; `targets.toml`'s own writer
    # (`write_targets()`, a later slice) is this file's only producer and always
    # emits this exact shape, so a `cast` here documents the trusted shape rather
    # than re-validating it defensively — matches config.py's own `cast` usage for
    # trusted, internally-produced TOML shapes.
    contexts = cast("dict[str, str]", raw.get("contexts", {}))
    current_context = cast("str | None", raw.get("current_context"))

    return TargetsFile(current_context=current_context, contexts=contexts)


def write_targets(config_dir: Path, targets: TargetsFile) -> None:
    """Serialize `targets` to `<config_dir>/targets.toml`, creating `config_dir` if needed.

    Uses `toml_writer`'s hand-rolled escaper/formatter (D16) rather than a new
    TOML-writing dependency. `current_context=None` omits the `current_context` line
    entirely (not `current_context = ""`) so it round-trips back to `None`, not an
    empty string, matching `load_targets()`'s own None-vs-empty-string distinction.
    See PLAN.md (issue #56) §1 D1, D16.
    """
    config_dir.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    if targets.current_context is not None:
        lines.append(f'current_context = "{escape_basic_string(targets.current_context)}"\n')
    lines.append(format_flat_table("contexts", targets.contexts))

    targets_path = config_dir / _TARGETS_FILE_NAME
    targets_path.write_text("\n".join(lines), encoding="utf-8")
