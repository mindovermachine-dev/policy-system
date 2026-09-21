"""ps-cli target/context configuration: `targets.toml` (named contexts) resolution.

New module introduced by issue #56 (multi-target config model). `targets.toml` maps
named contexts (e.g. "dev", "prod") to PS Service URLs and records which context is
currently selected (`current_context`). See PLAN.md (issue #56) §1 D1 for the file
schema, D2 for config-directory resolution, and D4 for why a malformed `targets.toml`
raises `PsCliError` (unlike `ps-cli.toml`'s unwrapped-crash behavior today).

Issue #57 Slice 1 (D-57-2) rewrites the `[contexts]` table's shape from flat
`name = "url"` strings to nested `[contexts.<name>]` tables (`url` required, an
optional `[contexts.<name>.auth]` sub-table for per-context OIDC parameter overrides).
`ContextEntry`/`AuthOverrides` are the parsed shape; a pre-#57 flat-string context
value raises `PsCliError` naming the file, pointing the operator at `config
set-context` to rewrite it, rather than an unhandled `AttributeError`/`TypeError`.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ps_cli.errors import PsCliError
from ps_cli.toml_writer import escape_basic_string, format_string_array

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
class AuthOverrides:
    """Per-context OIDC parameter overrides, layered onto device-flow discovery.

    Every field is `None` when not overridden by the operator -- `write_targets()`
    omits a `None` field's line entirely rather than writing it empty. See PLAN.md
    (issue #57) §2 Slice 1, D-57-2. Override-resolution logic (merging these onto
    discovered defaults) is a later slice's job -- this dataclass only carries the
    parsed shape.
    """

    issuer: str | None
    client_id: str | None
    scopes: tuple[str, ...] | None
    audience: str | None


@dataclass(frozen=True)
class ContextEntry:
    """One named context's PS Service URL, plus any per-context auth overrides."""

    url: str
    auth: AuthOverrides | None


@dataclass(frozen=True)
class TargetsFile:
    """Parsed contents of `targets.toml`: named contexts plus the currently-selected one.

    `current_context` is `None` when the key is absent from the file (no context has
    been selected yet) — distinct from an explicitly-empty string. See PLAN.md
    (issue #56) §1 D1.
    """

    current_context: str | None
    contexts: dict[str, ContextEntry]


def _parse_auth_overrides(raw_auth: dict[str, object]) -> AuthOverrides:
    """Parse a `[contexts.<name>.auth]` table into `AuthOverrides`."""
    raw_scopes = raw_auth.get("scopes")
    scopes = tuple(cast("list[str]", raw_scopes)) if raw_scopes is not None else None
    return AuthOverrides(
        issuer=cast("str | None", raw_auth.get("issuer")),
        client_id=cast("str | None", raw_auth.get("client_id")),
        scopes=scopes,
        audience=cast("str | None", raw_auth.get("audience")),
    )


def _parse_context_entry(
    targets_path: Path, name: str, raw_value: object, *, strict: bool
) -> ContextEntry | None:
    """Parse one `[contexts.<name>]` table into a `ContextEntry`.

    Raises `PsCliError` naming `targets_path` if `raw_value` is not a table -- the
    pre-issue-#57 shape wrote a bare string (`dev = "http://..."`) directly under
    `[contexts]`; a deliberate, explicit "old targets.toml format" error instead of a
    raw `AttributeError`/`TypeError` from treating a `str` as a `dict` (L1 "Fail Fast
    at Boundaries"). See PLAN.md (issue #57) §2 Slice 1, D-57-2.

    When `strict=False`, an old-format entry is skipped (returns `None`) with a
    warning to stderr instead of raising -- the seam `set_context`'s own recovery
    path (`handle_config_set_context`) needs: fixing *one* context by name must not
    be blocked by some *other*, unrelated context still being in the old format,
    otherwise the error's own hint ("re-run `set-context` for each context") is
    impossible to follow one context at a time.
    """
    if not isinstance(raw_value, dict):
        if not strict:
            print(
                f"⚠️  {targets_path} uses the old targets.toml format for context "
                f"'{name}' (a plain string, not a table) -- dropping it; "
                f"re-run 'ps-cli config set-context {name} --url <url>' to restore it",
                file=sys.stderr,
            )
            return None
        raise PsCliError(
            msg=(
                f"{targets_path} uses the old targets.toml format for context "
                f"'{name}' (a plain string, not a table)"
            ),
            hint="re-run 'ps-cli config set-context' for each context to rewrite the file",
        )
    raw_table = cast("dict[str, object]", raw_value)
    url = cast("str", raw_table["url"])
    raw_auth = raw_table.get("auth")
    auth = (
        _parse_auth_overrides(cast("dict[str, object]", raw_auth)) if raw_auth is not None else None
    )
    return ContextEntry(url=url, auth=auth)


def load_targets(config_dir: Path, *, strict: bool = True) -> TargetsFile | None:
    """Load and parse `<config_dir>/targets.toml`.

    Returns `None` if the file does not exist — `targets.toml` is entirely optional;
    a caller falls through to the legacy resolver in that case (see PLAN.md
    (issue #56) §1 D3, case 4). Raises `PsCliError` naming the file path if the file
    exists but contains invalid TOML (AC-BI-010) — a deliberate divergence from
    `ps-cli.toml`'s unwrapped-crash behavior for malformed TOML today: `targets.toml`
    is operator-hand-edited, so a parse failure here is a user-facing error, not a
    packaging/environment bug. See PLAN.md (issue #56) §1 D4.

    With the default `strict=True`, also raises `PsCliError` if any context's value
    is the pre-issue-#57 flat-string shape (see `_parse_context_entry`) -- correct
    for every read-only or select-a-context caller (`load_config()`,
    `use-context`), which must not silently proceed against a file it cannot fully
    trust. `handle_config_set_context()` passes `strict=False`: its whole job is
    fixing one named context, which an unrelated old-format sibling must not block
    (see `_parse_context_entry`'s own docstring) -- old-format entries are dropped
    with a warning rather than blocking the write.
    """
    targets_path = config_dir / _TARGETS_FILE_NAME
    if not targets_path.is_file():
        return None

    try:
        raw = tomllib.loads(targets_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PsCliError(msg=f"{targets_path} contains invalid TOML: {exc}") from exc

    # `tomllib.loads` returns `dict[str, Any]`; `targets.toml`'s own writer
    # (`write_targets()`) is this file's only producer of the current schema and
    # always emits this exact shape -- a `cast` here documents the trusted shape
    # rather than re-validating it defensively, matching `config.py`'s own `cast`
    # usage for trusted, internally-produced TOML shapes. A context value that is
    # *not* this trusted shape (the pre-#57 flat string) is still explicitly
    # detected and rejected (or, non-strict, dropped) by `_parse_context_entry`
    # above, not silently cast away.
    raw_contexts = cast("dict[str, object]", raw.get("contexts", {}))
    parsed = {
        name: _parse_context_entry(targets_path, name, raw_value, strict=strict)
        for name, raw_value in raw_contexts.items()
    }
    contexts = {name: entry for name, entry in parsed.items() if entry is not None}
    current_context = cast("str | None", raw.get("current_context"))

    return TargetsFile(current_context=current_context, contexts=contexts)


def _format_auth_table(name: str, auth: AuthOverrides) -> str:
    """Render `[contexts.<name>.auth]`, one line per non-`None` `AuthOverrides` field.

    Hand-rolled inline rather than via a shared generic nested-table writer -- see
    PLAN.md (issue #57) Slice 1's DRY-threshold citation (`http_client.py:289-291`):
    only two nested-table call sites exist in this codebase (this one, and
    `credentials.py`'s per-context `[credentials.<context>]` token-bundle table),
    under this codebase's own stated "extract... once a pattern repeats a third
    time" threshold. Fields are emitted alphabetically for deterministic,
    diff-friendly output, mirroring `format_flat_table`'s sorted-key convention.
    """
    lines = [f"[contexts.{name}.auth]"]
    if auth.audience is not None:
        lines.append(f'audience = "{escape_basic_string(auth.audience)}"')
    if auth.client_id is not None:
        lines.append(f'client_id = "{escape_basic_string(auth.client_id)}"')
    if auth.issuer is not None:
        lines.append(f'issuer = "{escape_basic_string(auth.issuer)}"')
    if auth.scopes is not None:
        lines.append(f"scopes = {format_string_array(auth.scopes)}")
    return "\n".join(lines) + "\n"


def write_targets(config_dir: Path, targets: TargetsFile) -> None:
    """Serialize `targets` to `<config_dir>/targets.toml`, creating `config_dir` if needed.

    Uses `toml_writer`'s hand-rolled escaper/array-formatter (D16) rather than a new
    TOML-writing dependency. `current_context=None` omits the `current_context` line
    entirely (not `current_context = ""`) so it round-trips back to `None`, not an
    empty string, matching `load_targets()`'s own None-vs-empty-string distinction.
    Each context is written as a nested `[contexts.<name>]` table (`url`, required),
    followed by `[contexts.<name>.auth]` only when `auth` is not `None` -- a `None`
    `AuthOverrides` field is omitted entirely, mirroring `current_context`'s own
    None-omission convention. `url` is never omitted -- always required. Contexts are
    written sorted by name for deterministic, diff-friendly output. See PLAN.md
    (issue #56) §1 D1, D16; PLAN.md (issue #57) §2 Slice 1, D-57-2.
    """
    config_dir.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    if targets.current_context is not None:
        lines.append(f'current_context = "{escape_basic_string(targets.current_context)}"\n')
    for name in sorted(targets.contexts):
        entry = targets.contexts[name]
        lines.append(f'[contexts.{name}]\nurl = "{escape_basic_string(entry.url)}"\n')
        if entry.auth is not None:
            lines.append(_format_auth_table(name, entry.auth))

    targets_path = config_dir / _TARGETS_FILE_NAME
    targets_path.write_text("\n".join(lines), encoding="utf-8")
