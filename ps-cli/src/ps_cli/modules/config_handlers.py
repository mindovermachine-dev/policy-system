"""ps-cli `config` command handlers and `CONFIG_DISPATCH`.

Same shape as `ps_cli.modules.handlers` (handler functions plus a dispatch dict),
but kept in its own module and table (`CONFIG_DISPATCH`, not `DISPATCH`) on purpose:
`config` subcommands manage `targets.toml` and the stored credentials only, and must
never construct a `PsServiceClient` or call `load_config()`. `get-contexts` must stay
usable even when `load_config()` would raise, and `set-context`/`use-context` must
never trigger `PsServiceClient.__init__`'s insecure-URL warning for a command that
never contacts PS Service. `ps_cli.cli.run()` branches on `command in CONFIG_DISPATCH`
before ever calling `load_config()`.

`handle_config_set_context()` never writes a credential into `targets.toml`
(`TargetsFile` has no field for one) and always deletes the context's stored tokens
after writing.
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
    """Merge the four `--auth-*` flags onto `existing`, per field, with a passed flag winning.

    A flag's value is used only when that flag was passed this call (non-`None`); an
    omitted flag preserves whatever `existing` already had for that field, or stays
    `None` if `existing` is `None` too. The result is never an all-`None`
    `AuthOverrides`, so a new context with no `--auth-*` flags does not get an empty
    `[contexts.<name>.auth]` table written.

    Args:
        existing: The context's current auth overrides, or `None` if it has none.
        auth_issuer: New OIDC issuer, or `None` to keep the existing value.
        auth_client_id: New OIDC client id, or `None` to keep the existing value.
        auth_scopes: New OIDC scopes, or `None` to keep the existing value.
        auth_audience: New OIDC audience, or `None` to keep the existing value.

    Returns:
        The merged overrides, or `None` when neither `existing` nor any flag supplies
        a value.
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

    Loads any existing `targets.toml` (or starts from an empty context table), sets
    `contexts[name]` to the new URL with the merged auth overrides, and writes the
    file back. `current_context` is preserved unchanged: switching the selected
    context is `use-context`'s job. The auth flags are merged per field onto the
    context's existing `auth` (only fields whose flag was passed are overwritten;
    `auth` stays `None` when nothing supplies a value). An old-format context
    elsewhere in the file does not block the write: it is dropped with a warning
    rather than failing, and round-trips once its own `set-context` is run.

    After the write it always calls `credential_store.delete_tokens(name)`, including
    for a brand-new context name, so a stale credential from a previous URL is never
    reused against the new one.

    Args:
        name: The context name; its format is already validated by the parser.
        url: The PS Service URL for the context; already validated as http(s).
        config_dir: Directory holding `targets.toml`, or `None` for
            `resolve_config_dir()`.
        credential_store: Store whose tokens for `name` are deleted, or `None` for
            `build_credential_store()`.
        auth_issuer: OIDC issuer override, or `None` to leave it unchanged.
        auth_client_id: OIDC client id override, or `None` to leave it unchanged.
        auth_scopes: OIDC scopes override, or `None` to leave it unchanged.
        auth_audience: OIDC audience override, or `None` to leave it unchanged.

    Returns:
        None.

    Raises:
        PsCliError: `targets.toml` holds invalid TOML, or the credential store
            cannot be built or written.
        OSError: The configuration directory or `targets.toml` cannot be written.

    Side Effects:
        Writes `targets.toml` and overwrites the stored token bundle for `name`.
    """
    resolved_config_dir = config_dir if config_dir is not None else resolve_config_dir()
    store = credential_store if credential_store is not None else build_credential_store()

    # strict=False: fixing (or adding) *this* context by name must not be blocked
    # by some *other*, unrelated context still being in the older flat format.
    # An old-format sibling is dropped (with a warning) rather than blocking this
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

    `name` must already be defined in `[contexts]`; `[contexts]` itself is preserved
    unchanged and only `current_context` is rewritten.

    Args:
        name: The context to select; its format is already validated by the parser.
        config_dir: Directory holding `targets.toml`, or `None` for
            `resolve_config_dir()`.

    Returns:
        None.

    Raises:
        PsCliError: `name` is not a defined context (the error names it and lists
            every valid context, sorted), or `targets.toml` is invalid.
        OSError: `targets.toml` cannot be written.

    Side Effects:
        Rewrites `targets.toml`.
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
    """Render the "Auth Overrides" column value for one context: field names only.

    The column answers "what is overridden", not "what are the values", which keeps
    the table narrow however long an issuer URL is. Fields are listed in a fixed
    order (issuer, client_id, scopes, audience) for deterministic output.

    Args:
        auth: The context's auth overrides, or `None` if it has none.

    Returns:
        `"-"` when `auth` is `None` or sets no field; otherwise the comma-joined
        names of the fields that are set, e.g. `"issuer, audience"`.
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
    """Print every context as a bordered table, marking the currently selected one.

    Prints nothing when `targets.toml` is absent or empty: nothing configured yet is
    not a failure. Rows are sorted by context name, with columns "Current" (`"*"` for
    `current_context`, a space otherwise), "Name", "URL" and "Auth Overrides" (the
    names of the `auth.*` fields that are set, never their values).

    Args:
        config_dir: Directory holding `targets.toml`, or `None` for
            `resolve_config_dir()`.

    Returns:
        None.

    Raises:
        PsCliError: `targets.toml` holds invalid TOML or an old-format context.

    Side Effects:
        Reads `targets.toml` and prints the table to stdout.
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
    """Adapt `handle_config_set_context`'s signature to the `CONFIG_DISPATCH` shape.

    Args:
        args: Parsed arguments; `name`, `url` and the four `auth_*` values are read.

    Returns:
        None.

    Raises:
        PsCliError: Propagated from `handle_config_set_context`.
        OSError: Propagated from `handle_config_set_context`.

    Side Effects:
        Those of `handle_config_set_context`: rewrites `targets.toml` and deletes the
        context's stored tokens.
    """
    handle_config_set_context(
        cast("str", args.name),
        cast("str", args.url),
        auth_issuer=cast("str | None", args.auth_issuer),
        auth_client_id=cast("str | None", args.auth_client_id),
        auth_scopes=cast("tuple[str, ...] | None", args.auth_scopes),
        auth_audience=cast("str | None", args.auth_audience),
    )


def _dispatch_config_use_context(args: argparse.Namespace) -> None:
    """Adapt `handle_config_use_context`'s signature to the `CONFIG_DISPATCH` shape.

    Args:
        args: Parsed arguments; `name` is read.

    Returns:
        None.

    Raises:
        PsCliError: Propagated from `handle_config_use_context`.
        OSError: Propagated from `handle_config_use_context`.

    Side Effects:
        Rewrites `targets.toml`.
    """
    handle_config_use_context(cast("str", args.name))


def _dispatch_config_get_contexts(args: argparse.Namespace) -> None:
    """Adapt `handle_config_get_contexts`'s signature to the `CONFIG_DISPATCH` shape.

    Args:
        args: Parsed arguments; unused, since `get-contexts` takes none.

    Returns:
        None.

    Raises:
        PsCliError: Propagated from `handle_config_get_contexts`.

    Side Effects:
        Reads `targets.toml` and prints to stdout.
    """
    del args
    handle_config_get_contexts()


CONFIG_DISPATCH: dict[str, Callable[[argparse.Namespace], None]] = {
    "config_set_context": _dispatch_config_set_context,
    "config_use_context": _dispatch_config_use_context,
    "config_get_contexts": _dispatch_config_get_contexts,
}
