"""ps-cli `config` command handlers and `CONFIG_DISPATCH` (issue #56, PLAN.md §1 D7/D8/D13).

Mirrors `ps_cli.modules.handlers`' shape (handler functions plus a dispatch dict), but is
kept in its own module and dispatch table (`CONFIG_DISPATCH`, not `DISPATCH`) per PLAN.md
D8's flagged, deliberate deviation: `config` subcommands manage `targets.toml`/
`credentials.toml` only -- they must never construct a `PsServiceClient` or call
`load_config()` (see D8's two concrete reasons: `list-contexts` must stay usable even when
`load_config()` would raise, and `set-context`/`use-context` must never trigger
`PsServiceClient.__init__`'s "insecure URL" side-effect warning for a command that never
contacts PS Service). `ps_cli.cli.run()` branches on `command in CONFIG_DISPATCH` before
ever calling `load_config()` -- the architectural boundary this module exists to satisfy.

Slice 22 adds `handle_config_set_context()`'s `targets.toml` write (AC-BI-012: the file it
writes never contains a credential -- structurally guaranteed, since `TargetsFile`/
`write_targets()` have no field to put one in). Slice 23 adds the unconditional
`credential_store.delete_credential()` call (AC-BI-014, D13). Slice 24 wires this module's
`CONFIG_DISPATCH` into `ps_cli.cli.run()`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ps_cli.credentials import build_credential_store
from ps_cli.errors import assert_contract
from ps_cli.targets import TargetsFile, load_targets, resolve_config_dir, write_targets

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable
    from pathlib import Path

    from ps_cli.credentials import CredentialStore


def handle_config_set_context(
    name: str,
    url: str,
    *,
    config_dir: Path | None = None,
    credential_store: CredentialStore | None = None,
) -> None:
    """Create or update named context `name`'s PS Service URL to `url` in `targets.toml`.

    Loads any existing `targets.toml` (or starts from an empty context table if none
    exists yet), sets/overwrites `contexts[name] = url`, and writes it back --
    `current_context` is read and preserved unchanged, never touched by this handler
    (PLAN.md D1: switching the *selected* context is `use-context`'s job, a later slice).
    The written file structurally cannot contain a credential (AC-BI-012) -- `TargetsFile`
    has no field for one.

    After the write, unconditionally calls `credential_store.delete_credential(name)` --
    every call, including the very first `set-context` for a brand-new context name
    (AC-BI-014, D13) -- so a stale credential from a previous URL is never silently reused
    against the new one. `config_dir` defaults to `resolve_config_dir()` and
    `credential_store` to `build_credential_store(resolved_config_dir)` when omitted,
    exactly like a real CLI invocation with no explicit overrides -- the same
    constructor-injection seam `config.py`'s `load_config()` already establishes (PLAN.md
    D2).
    """
    resolved_config_dir = config_dir if config_dir is not None else resolve_config_dir()
    store = (
        credential_store
        if credential_store is not None
        else build_credential_store(resolved_config_dir)
    )

    existing = load_targets(resolved_config_dir)
    contexts = dict(existing.contexts) if existing is not None else {}
    contexts[name] = url
    current_context = existing.current_context if existing is not None else None

    write_targets(
        resolved_config_dir,
        TargetsFile(current_context=current_context, contexts=contexts),
    )

    store.delete_credential(name)


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


def handle_config_list_contexts(*, config_dir: Path | None = None) -> None:
    """Print every context, one per line, marking the currently-selected one.

    Loads `targets.toml` (prints nothing if absent or empty -- nothing configured yet is
    not a failure state, no AC requires otherwise). For each context name, sorted
    alphabetically, prints `f"{marker} {name}  {url}"` where `marker` is `"*"` for
    `current_context` and `" "` otherwise (AC-BI-007). `config_dir` defaults to
    `resolve_config_dir()` when omitted, the same seam every other handler here uses.
    """
    resolved_config_dir = config_dir if config_dir is not None else resolve_config_dir()
    targets = load_targets(resolved_config_dir)
    if targets is None:
        return

    for name in sorted(targets.contexts):
        marker = "*" if name == targets.current_context else " "
        print(f"{marker} {name}  {targets.contexts[name]}")


def _dispatch_config_set_context(args: argparse.Namespace) -> None:
    """Adapt `handle_config_set_context`'s signature to the `CONFIG_DISPATCH` shape."""
    handle_config_set_context(cast("str", args.name), cast("str", args.url))


def _dispatch_config_use_context(args: argparse.Namespace) -> None:
    """Adapt `handle_config_use_context`'s signature to the `CONFIG_DISPATCH` shape."""
    handle_config_use_context(cast("str", args.name))


def _dispatch_config_list_contexts(args: argparse.Namespace) -> None:
    """Adapt `handle_config_list_contexts`'s signature to the `CONFIG_DISPATCH` shape."""
    del args
    handle_config_list_contexts()


CONFIG_DISPATCH: dict[str, Callable[[argparse.Namespace], None]] = {
    "config_set_context": _dispatch_config_set_context,
    "config_use_context": _dispatch_config_use_context,
    "config_list_contexts": _dispatch_config_list_contexts,
}
