"""ps-cli `config` command handlers and `CONFIG_DISPATCH` (issue #56, PLAN.md §1 D7/D8/D13).

Mirrors `ps_cli.modules.handlers`' shape (handler functions plus a dispatch dict), but is
kept in its own module and dispatch table (`CONFIG_DISPATCH`, not `DISPATCH`) per PLAN.md
D8's flagged, deliberate deviation: `config` subcommands manage `targets.toml`/
`credentials.toml` only -- they must never construct a `PsServiceClient` or call
`load_config()` (see D8's two concrete reasons: `get-contexts` must stay usable even when
`load_config()` would raise, and `set-context`/`use-context` must never trigger
`PsServiceClient.__init__`'s "insecure URL" side-effect warning for a command that never
contacts PS Service). `ps_cli.cli.run()` branches on `command in CONFIG_DISPATCH` before
ever calling `load_config()` -- the architectural boundary this module exists to satisfy.

Slice 22 adds `handle_config_set_context()`'s `targets.toml` write (AC-BI-012: the file it
writes never contains a credential -- structurally guaranteed, since `TargetsFile`/
`write_targets()` have no field to put one in). Slice 23 adds the unconditional
`credential_store.delete_tokens()` call (AC-BI-014, D13). Slice 24 wires this module's
`CONFIG_DISPATCH` into `ps_cli.cli.run()`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ps_cli import render
from ps_cli.credentials import build_credential_store
from ps_cli.errors import assert_contract
from ps_cli.targets import (
    AuthOverrides,
    ContextEntry,
    TargetsFile,
    load_targets,
    resolve_config_dir,
    write_targets,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable
    from pathlib import Path

    from ps_cli.credentials import CredentialStore


def _merge_auth_overrides(
    existing: AuthOverrides | None,
    *,
    auth_issuer: str | None,
    auth_client_id: str | None,
    auth_scopes: tuple[str, ...] | None,
    auth_audience: str | None,
) -> AuthOverrides | None:
    """Merge the four `--auth-*` flags onto `existing`, per-field, flag-passed-wins.

    AC-BI-006: "the corresponding `auth.*` key is written" -- singular, per-flag, not
    "the whole auth table is replaced". A flag's value is used only when that flag was
    actually passed this call (non-`None`); an omitted flag preserves whatever
    `existing` already had for that field (or stays `None` if `existing` is `None`
    too). Returns `None` outright -- never an all-`None` `AuthOverrides` -- when
    neither `existing` nor any of the four flags supplies a value, so a
    brand-new context with no `--auth-*` flags never gets a gratuitous empty
    `[contexts.<name>.auth]` table written.
    """
    issuer = auth_issuer if auth_issuer is not None else (existing.issuer if existing else None)
    client_id = (
        auth_client_id if auth_client_id is not None else (existing.client_id if existing else None)
    )
    scopes = auth_scopes if auth_scopes is not None else (existing.scopes if existing else None)
    audience = (
        auth_audience if auth_audience is not None else (existing.audience if existing else None)
    )
    if issuer is None and client_id is None and scopes is None and audience is None:
        return None
    return AuthOverrides(issuer=issuer, client_id=client_id, scopes=scopes, audience=audience)


def handle_config_set_context(
    name: str,
    url: str,
    *,
    config_dir: Path | None = None,
    credential_store: CredentialStore | None = None,
    auth_issuer: str | None = None,
    auth_client_id: str | None = None,
    auth_scopes: tuple[str, ...] | None = None,
    auth_audience: str | None = None,
) -> None:
    """Create or update named context `name`'s PS Service URL to `url` in `targets.toml`.

    Loads any existing `targets.toml` (or starts from an empty context table if none
    exists yet), sets/overwrites `contexts[name] = ContextEntry(url=url, auth=<merged>)`,
    and writes it back -- `current_context` is read and preserved unchanged, never
    touched by this handler (PLAN.md D1: switching the *selected* context is
    `use-context`'s job, a later slice). The written file structurally cannot contain a
    credential (AC-BI-012) -- `TargetsFile`/`ContextEntry` have no field for one.

    `auth_issuer`/`auth_client_id`/`auth_scopes`/`auth_audience` (issue #57 Slice 8,
    AC-BI-006) are merged onto the context's existing `auth` (if any) per-field via
    `_merge_auth_overrides()`: only the fields whose corresponding flag was actually
    passed this call are overwritten, every other field is preserved unchanged, and
    `auth` stays `None` when nothing passed and nothing pre-existing supplies a value.

    After the write, unconditionally calls `credential_store.delete_tokens(name)` --
    every call, including the very first `set-context` for a brand-new context name
    (AC-BI-014, D13) -- so a stale credential from a previous URL is never silently reused
    against the new one. `config_dir` defaults to `resolve_config_dir()` and
    `credential_store` to `build_credential_store()` when omitted, exactly like a real
    CLI invocation with no explicit overrides -- the same constructor-injection seam
    `config.py`'s `load_config()` already establishes (PLAN.md D2). Issue #121:
    `build_credential_store()` is zero-argument -- `resolved_config_dir` is used only
    for `targets.toml`'s own read/write below, not for credential resolution any more.
    """
    resolved_config_dir = config_dir if config_dir is not None else resolve_config_dir()
    store = credential_store if credential_store is not None else build_credential_store()

    # strict=False: fixing (or adding) *this* context by name must not be blocked
    # by some *other*, unrelated context still being in the pre-#57 old format --
    # see `targets.load_targets`/`_parse_context_entry`'s own docstrings. An
    # old-format sibling is dropped (with a warning) rather than blocking this
    # write; it round-trips back once its own `set-context` call is made.
    existing = load_targets(resolved_config_dir, strict=False)
    contexts = dict(existing.contexts) if existing is not None else {}
    existing_entry = contexts.get(name)
    merged_auth = _merge_auth_overrides(
        existing_entry.auth if existing_entry is not None else None,
        auth_issuer=auth_issuer,
        auth_client_id=auth_client_id,
        auth_scopes=auth_scopes,
        auth_audience=auth_audience,
    )
    contexts[name] = ContextEntry(url=url, auth=merged_auth)
    current_context = existing.current_context if existing is not None else None

    write_targets(
        resolved_config_dir,
        TargetsFile(current_context=current_context, contexts=contexts),
    )

    store.delete_tokens(name)


def handle_config_use_context(name: str, *, config_dir: Path | None = None) -> None:
    """Select `name` as the current context, persisting it into `targets.toml`.

    Loads `targets.toml`, validating `name` is a member of `[contexts]` --
    `assert_contract(name in targets.contexts, ...)`, raising `PsCliError` naming `name`
    and listing every valid context name, sorted, otherwise (AC-BI-009's command half).
    On success, writes `targets.toml` back with `current_context` set to `name` --
    `[contexts]` itself is read and preserved unchanged, never touched by this handler.
    `config_dir` defaults to `resolve_config_dir()` when omitted, the same
    constructor-injection seam `handle_config_set_context()` already establishes.
    """
    resolved_config_dir = config_dir if config_dir is not None else resolve_config_dir()
    existing = load_targets(resolved_config_dir)
    contexts = existing.contexts if existing is not None else {}

    assert_contract(
        contract=name in contexts,
        msg=f"context '{name}' is not defined in targets.toml",
        hint=(
            f"valid contexts: {', '.join(sorted(contexts))}"
            if contexts
            else "no contexts are defined in targets.toml"
        ),
    )

    write_targets(
        resolved_config_dir,
        TargetsFile(current_context=name, contexts=contexts),
    )


def _auth_overrides_summary(auth: AuthOverrides | None) -> str:
    """Render the "Auth Overrides" column value for one context: field *names* only.

    `"-"` when `auth` is `None`; otherwise a comma-joined list of which `auth.*`
    fields are set (e.g. `"issuer, audience"`) -- never the literal values (issue #57
    Slice 8): this column's job is "what's overridden," not "what the values are,"
    keeping the table's width bounded regardless of how long an issuer URL is. Fields
    are listed in a fixed order (issuer, client_id, scopes, audience) for
    deterministic, diff-friendly output.
    """
    if auth is None:
        return "-"
    set_fields = [
        field_name
        for field_name, value in (
            ("issuer", auth.issuer),
            ("client_id", auth.client_id),
            ("scopes", auth.scopes),
            ("audience", auth.audience),
        )
        if value is not None
    ]
    return ", ".join(set_fields) if set_fields else "-"


def handle_config_get_contexts(*, config_dir: Path | None = None) -> None:
    """Print every context as a bordered table, marking the currently-selected one.

    Loads `targets.toml` (prints nothing if absent or empty -- nothing configured yet is
    not a failure state, no AC requires otherwise). Rows are sorted alphabetically by
    context name, with columns "Current" (``"*"`` for `current_context`, `" "`
    otherwise), "Name", "URL", "Auth Overrides" (issue #57 Slice 8: which `auth.*`
    field *names* are set, never their values -- see `_auth_overrides_summary()`),
    rendered via `render.print_table()`. `config_dir` defaults to
    `resolve_config_dir()` when omitted, the same seam every other handler here uses.
    """
    resolved_config_dir = config_dir if config_dir is not None else resolve_config_dir()
    targets = load_targets(resolved_config_dir)
    if targets is None:
        return
    rows = [
        [
            "*" if name == targets.current_context else " ",
            name,
            targets.contexts[name].url,
            _auth_overrides_summary(targets.contexts[name].auth),
        ]
        for name in sorted(targets.contexts)
    ]
    render.print_table(["Current", "Name", "URL", "Auth Overrides"], rows)


def _dispatch_config_set_context(args: argparse.Namespace) -> None:
    """Adapt `handle_config_set_context`'s signature to the `CONFIG_DISPATCH` shape."""
    handle_config_set_context(
        cast("str", args.name),
        cast("str", args.url),
        auth_issuer=cast("str | None", args.auth_issuer),
        auth_client_id=cast("str | None", args.auth_client_id),
        auth_scopes=cast("tuple[str, ...] | None", args.auth_scopes),
        auth_audience=cast("str | None", args.auth_audience),
    )


def _dispatch_config_use_context(args: argparse.Namespace) -> None:
    """Adapt `handle_config_use_context`'s signature to the `CONFIG_DISPATCH` shape."""
    handle_config_use_context(cast("str", args.name))


def _dispatch_config_get_contexts(args: argparse.Namespace) -> None:
    """Adapt `handle_config_get_contexts`'s signature to the `CONFIG_DISPATCH` shape."""
    del args
    handle_config_get_contexts()


CONFIG_DISPATCH: dict[str, Callable[[argparse.Namespace], None]] = {
    "config_set_context": _dispatch_config_set_context,
    "config_use_context": _dispatch_config_use_context,
    "config_get_contexts": _dispatch_config_get_contexts,
}
