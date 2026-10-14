"""ps-cli `auth` command handlers and `AUTH_DISPATCH`.

Connects OIDC discovery, device-flow login and credential storage to the CLI surface.
Follows the shape of `config_handlers.py` (handler functions plus a dispatch dict) but
lives in its own module and table (`AUTH_DISPATCH`, not `DISPATCH`/`CONFIG_DISPATCH`)
for the same reason: `ps_cli.cli._dispatch_command` must route `auth` subcommands here
before ever reaching `_resolve_client`. `_resolve_client`'s `PsServiceClient` requires
an already-valid token for every business call, so `auth login` would be circular if it
had to go through that path.

Commands: `login` (device-flow login, stores the refresh token), `status` (verifies the
stored credential end to end) and `logout` (deletes the stored credential).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

from ps_cli import device_flow, oidc_discovery
from ps_cli.config import load_config
from ps_cli.credentials import build_credential_store
from ps_cli.errors import CannotVerifyError, CredentialStoreError, PsCliError
from ps_cli.targets import resolve_auth_override, resolve_config_dir

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable
    from pathlib import Path

    import httpx

    from ps_cli.config import CliConfig
    from ps_cli.credentials import CredentialStore
    from ps_cli.device_flow import DeviceAuthorization
    from ps_cli.targets import AuthOverrides

_LOGIN_HINT = "run `ps-cli auth login`"

_NO_CONTEXT_TO_AUTHENTICATE_MSG = (
    "no context to authenticate; run `ps-cli config set-context <name> --url <url>` first"
)


def _print_device_authorization(device_auth: DeviceAuthorization) -> None:
    """Print what the user needs to complete login.

    Prints `verification_uri_complete` alone when the provider supplied one; otherwise
    `verification_uri` and `user_code` on their own lines. Used as
    `complete_device_login`'s `on_device_authorization` callback, so it runs right after
    the device-authorization request, before the blocking poll begins.
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
    """Log in to `context_name` via OIDC device flow and store the resulting tokens.

    Resolves the OIDC parameters, runs the device-flow login (printing the verification
    URI and user code as soon as they are issued, before the poll blocks), converts the
    tokens to a `TokenBundle` and stores it. On success prints
    `logged in to <context> (<issuer>)`.

    Args:
        context_name: The context to authenticate, or `None` when no `--context` was
            given and `targets.toml` has no `current_context`.
        config: The resolved CLI config; `service_url` is used for OIDC discovery.
        config_dir: Accepted for symmetry with the other handlers; not read, because
            `credential_store` and `auth_override` are already resolved by the caller.
        credential_store: Where the resulting refresh token is stored.
        auth_override: The context's `auth` overrides from `targets.toml`, or `None`.
        sleep: Called with the seconds to wait between token polls; injectable so tests
            never wait on a real clock.

    Raises:
        PsCliError: `context_name` is `None`; discovery, the device-authorization
            request or the poll failed; or the login expired or was denied.
        CannotVerifyError: PS Service or the IdP could not be reached, failed the TLS
            check, or answered 5xx during discovery.
        CredentialStoreError: The credential store could not be written.

    Side Effects:
        HTTPS requests to PS Service and the IdP, a blocking poll, a credential-store
        write, and output to stdout.
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


def handle_auth_status(
    context_name: str | None,
    config: CliConfig,
    *,
    credential_store: CredentialStore,
    auth_override: AuthOverrides | None,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Verify `context_name`'s stored credential end to end and print its status.

    The credential is verified by obtaining an access token through the same path every
    authenticated command uses, so "logged in" means a usable access token actually came
    back, and a rotated refresh token is persisted. On success prints `context`,
    `issuer` and `logged in`. No token value is ever printed.

    Args:
        context_name: The context to check, or `None` when none is selected.
        config: The resolved CLI config; `service_url` is used for OIDC discovery.
        credential_store: Where the stored credential is read from and rotated back to.
        auth_override: The context's `auth` overrides from `targets.toml`, or `None`.
        transport: Optional `httpx` transport that replaces the network (tests only).

    Raises:
        CredentialStoreError: The credential store could not be read, or written after a
            refresh; propagates unchanged.
        PsCliError: `context_name` is `None`; nothing is stored ("not logged in"); or the
            refresh was rejected ("not usable", hint `ps-cli auth login`).
        CannotVerifyError: Not raised to the caller; it is re-raised as a `PsCliError`
            reading "cannot verify" when the IdP or PS Service is unreachable or
            answers 5xx.

    Side Effects:
        HTTPS requests to PS Service and the IdP, possibly a credential-store write,
        and output to stdout.
    """
    if context_name is None:
        raise PsCliError(msg=_NO_CONTEXT_TO_AUTHENTICATE_MSG)
    bundle = credential_store.get_tokens(context_name)
    if bundle is None:
        raise PsCliError(msg=f"not logged in to '{context_name}'", hint=_LOGIN_HINT)
    try:
        device_flow.ensure_valid_access_token(
            context=context_name,
            service_url=config.service_url,
            auth_override=auth_override,
            credential_store=credential_store,
            access_token_cache=device_flow.AccessTokenCache(),
            transport=transport,
        )
    except CredentialStoreError:
        raise
    except CannotVerifyError as error:
        reason = f"{error.msg} ({error.detail})" if error.detail else error.msg
        raise PsCliError(
            msg=f"cannot verify the credential for '{context_name}': {reason}",
            hint="check the connection to PS Service and the identity provider, then retry",
        ) from error
    except PsCliError as error:
        raise PsCliError(
            msg=f"credential present for '{context_name}' but not usable: {error.msg}",
            hint=_LOGIN_HINT,
        ) from error
    print(f"context: {context_name}")
    print(f"issuer: {bundle.issuer}")
    print("logged in")


def handle_auth_logout(context_name: str | None, *, credential_store: CredentialStore) -> None:
    """Log `context_name` out by removing its stored credential.

    Deletion is real, not a flag, and is a no-op when nothing was stored for the
    context. Prints nothing on success.

    Args:
        context_name: The context to log out, or `None` when none is selected.
        credential_store: The store the credential is deleted from.

    Raises:
        PsCliError: `context_name` is `None`.
        CredentialStoreError: The credential store could not be written.

    Side Effects:
        Modifies the credential store.
    """
    if context_name is None:
        raise PsCliError(msg=_NO_CONTEXT_TO_AUTHENTICATE_MSG)
    credential_store.delete_tokens(context_name)


def _dispatch_auth_login(args: argparse.Namespace) -> None:
    """Adapt `handle_auth_login`'s signature to the `AUTH_DISPATCH` shape.

    Resolves the config, credential store and auth override itself rather than through
    `_resolve_client`, which would need the token that `auth login` is obtaining.
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

    Resolves the config, credential store and auth override itself rather than through
    `_resolve_client`, as `_dispatch_auth_login` does.
    """
    context = cast("str | None", getattr(args, "context", None))
    config_dir = resolve_config_dir()
    config = load_config(context=context, config_dir=config_dir)
    credential_store = build_credential_store()
    auth_override = resolve_auth_override(config, config_dir)
    handle_auth_status(
        config.context_name,
        config,
        credential_store=credential_store,
        auth_override=auth_override,
    )


def _dispatch_auth_logout(args: argparse.Namespace) -> None:
    """Adapt `handle_auth_logout`'s signature to the `AUTH_DISPATCH` shape.

    Resolves the config and credential store itself rather than through
    `_resolve_client`, as `_dispatch_auth_login` does.
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
