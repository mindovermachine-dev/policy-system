"""ps-cli `auth` command handlers and `AUTH_DISPATCH` (issue #57 Slices 13-20).

Ties Slices 5-12 (OIDC discovery, device-flow, credential storage) to the CLI
surface. Mirrors `config_handlers.py`'s own shape (handler functions plus a
dispatch dict), but kept in its own module and dispatch table (`AUTH_DISPATCH`,
not `DISPATCH`/`CONFIG_DISPATCH`) for the same reason `config_handlers.py`
exists separately: `ps_cli.cli._dispatch_command` must route `auth`
subcommands here *before* ever reaching `_resolve_client` -- after issue #57
Slice 15 (bearer attachment), `_resolve_client`'s underlying `PsServiceClient`
requires an *already-valid* token for every business call, which would make
`auth login` itself circular if it had to go through that same path.

`login` (Slice 13), `status` (Slice 19), and `logout` (Slice 20) all have real
implementations and `AUTH_DISPATCH` entries now. See IMPL_SLICE_13-14.md for
why `status`/`logout` carried placeholder `AUTH_DISPATCH` entries prior to
Slices 19/20 (raising an actionable "not yet implemented" `PsCliError` rather
than being left out of the dict entirely, which would otherwise fall through
to `_resolve_client`/`DISPATCH` and crash with an opaque `KeyError`).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

from ps_cli import device_flow, oidc_discovery
from ps_cli.config import load_config
from ps_cli.credentials import build_credential_store
from ps_cli.errors import PsCliError
from ps_cli.targets import resolve_auth_override, resolve_config_dir

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable
    from pathlib import Path

    from ps_cli.config import CliConfig
    from ps_cli.credentials import CredentialStore
    from ps_cli.device_flow import DeviceAuthorization
    from ps_cli.targets import AuthOverrides

_NO_CONTEXT_TO_AUTHENTICATE_MSG = (
    "no context to authenticate; run `ps-cli config set-context <name> --url <url>` first"
)


def _print_device_authorization(device_auth: DeviceAuthorization) -> None:
    """Print what the operator needs to complete login (AC-BI-007).

    Prints `verification_uri_complete` alone when the provider supplied one;
    otherwise `verification_uri` and `user_code` on their own lines. Called as
    `complete_device_login`'s `on_device_authorization` callback -- fires right
    after the device-authorization request, before the (blocking) poll begins.
    """
    if device_auth.verification_uri_complete is not None:
        print(device_auth.verification_uri_complete)
    else:
        print(device_auth.verification_uri)
        print(device_auth.user_code)


def handle_auth_login(
    context_name: str | None,
    config: CliConfig,
    *,
    config_dir: Path,
    credential_store: CredentialStore,
    auth_override: AuthOverrides | None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Log in to `context_name` via OIDC device-flow, storing the resulting tokens.

    Raises `PsCliError` up front if `context_name is None` (D-57-5's scope
    boundary -- there is no context to authenticate against, e.g. no
    `--context` given and no `current_context` set in `targets.toml`).
    Otherwise: resolves every OIDC parameter device-flow login needs
    (`oidc_discovery.resolve_auth_parameters`), runs the full device-flow login
    (`device_flow.complete_device_login`), printing the verification URI/user
    code the moment they are minted -- before the poll blocks
    (`_print_device_authorization`, wired as the `on_device_authorization`
    callback) -- converts the resulting `TokenResponse` into a `TokenBundle`
    (`device_flow.token_bundle_from_response`), and stores it via
    `credential_store.set_tokens`. Prints one confirmation line on success
    (AC-BI-010's CLI-visible half).

    `config_dir` is accepted for signature symmetry with the dispatch
    adapter's own dependency resolution (mirrors every `config_handlers.py`
    handler's `config_dir` parameter) but is not read here -- `credential_store`
    and `auth_override` are already resolved by the caller.
    """
    del config_dir
    if context_name is None:
        raise PsCliError(msg=_NO_CONTEXT_TO_AUTHENTICATE_MSG)

    params = oidc_discovery.resolve_auth_parameters(config.service_url, auth_override)
    token_response = device_flow.complete_device_login(
        params, sleep=sleep, on_device_authorization=_print_device_authorization
    )
    bundle = device_flow.token_bundle_from_response(token_response, params.issuer)
    credential_store.set_tokens(context_name, bundle)
    print(f"logged in to {context_name} ({params.issuer})")


def handle_auth_status(context_name: str | None, *, credential_store: CredentialStore) -> None:
    """Print `context_name`'s login status (AC-BI-015; issue #121 D-121-6).

    Raises `PsCliError` if `context_name is None` (D-57-5, same boundary as
    `handle_auth_login`). If `credential_store.get_tokens(context_name)` is `None`,
    prints `"not logged in to '{context_name}'"` and returns normally -- this is a
    legitimate status to report (exit code 0), not a failure, unlike `auth login`'s
    own D-57-5 "no context" case. Otherwise prints three lines: `context`, `issuer`,
    and `logged in`.

    Issue #121: no `subject`/`expiry` any more -- `TokenBundle` no longer carries an
    `access_token`/`expires_at` to derive either from (AC-BI-001), and deriving them
    would require a live refresh-token exchange purely to populate a status display,
    contradicting this command's own invariant of never contacting PS Service or the
    IdP -- reads only the local store.
    """
    if context_name is None:
        raise PsCliError(msg=_NO_CONTEXT_TO_AUTHENTICATE_MSG)
    bundle = credential_store.get_tokens(context_name)
    if bundle is None:
        print(f"not logged in to '{context_name}'")
        return
    print(f"context: {context_name}")
    print(f"issuer: {bundle.issuer}")
    print("logged in")


def handle_auth_logout(context_name: str | None, *, credential_store: CredentialStore) -> None:
    """Log `context_name` out (AC-BI-016).

    Raises `PsCliError` if `context_name is None` (D-57-5). Otherwise unconditionally
    calls `credential_store.delete_tokens(context_name)` -- a real removal, a no-op if
    nothing was stored for that context; there is no "inactive" flag anywhere in this
    design. Prints nothing on success (this command has nothing new to report).
    """
    if context_name is None:
        raise PsCliError(msg=_NO_CONTEXT_TO_AUTHENTICATE_MSG)
    credential_store.delete_tokens(context_name)


def _dispatch_auth_login(args: argparse.Namespace) -> None:
    """Adapt `handle_auth_login`'s signature to the `AUTH_DISPATCH` shape.

    Resolves `config`/`credential_store`/`auth_override` independently of
    `_resolve_client` -- `auth login` must never go through it (see this
    module's own docstring). Mirrors `config_handlers.py`'s dispatch adapters'
    own independent dependency resolution.
    """
    context = cast("str | None", getattr(args, "context", None))
    config_dir = resolve_config_dir()
    config = load_config(context=context, config_dir=config_dir)
    credential_store = build_credential_store()
    auth_override = resolve_auth_override(config, config_dir)
    handle_auth_login(
        config.context_name,
        config,
        config_dir=config_dir,
        credential_store=credential_store,
        auth_override=auth_override,
    )


def _dispatch_auth_status(args: argparse.Namespace) -> None:
    """Adapt `handle_auth_status`'s signature to the `AUTH_DISPATCH` shape.

    Resolves `config`/`credential_store` independently of `_resolve_client`, exactly
    like `_dispatch_auth_login` -- see this module's own docstring.
    """
    context = cast("str | None", getattr(args, "context", None))
    config_dir = resolve_config_dir()
    config = load_config(context=context, config_dir=config_dir)
    credential_store = build_credential_store()
    handle_auth_status(config.context_name, credential_store=credential_store)


def _dispatch_auth_logout(args: argparse.Namespace) -> None:
    """Adapt `handle_auth_logout`'s signature to the `AUTH_DISPATCH` shape.

    Resolves `config`/`credential_store` independently of `_resolve_client`, exactly
    like `_dispatch_auth_login` -- see this module's own docstring.
    """
    context = cast("str | None", getattr(args, "context", None))
    config_dir = resolve_config_dir()
    config = load_config(context=context, config_dir=config_dir)
    credential_store = build_credential_store()
    handle_auth_logout(config.context_name, credential_store=credential_store)


AUTH_DISPATCH: dict[str, Callable[[argparse.Namespace], None]] = {
    "auth_login": _dispatch_auth_login,
    "auth_status": _dispatch_auth_status,
    "auth_logout": _dispatch_auth_logout,
}
