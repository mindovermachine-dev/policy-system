"""ps-cli's command-line entry point: argument parsing, dispatch, and the error boundary.

`run()` is the testable core: it parses `argv`, builds a `PsServiceClient` from
`ps_cli.config.load_config()` only when one is not injected, dispatches to the
matching handler through the dispatch tables, and is the main `try/except` in the
call chain around a real subcommand dispatch. That one `try` has three sibling
`except` clauses: `PsCliError` (exit 1), `KeyboardInterrupt` (the operator
cancelled, exit 130), and any other `Exception` (an unhandled bug, exit 2,
message-only unless `-v`). This is a deliberate, narrow exception to the L2
ps-cli "let bugs crash" principle, scoped to this one dispatch boundary (see the
`## ps-cli` Error Handling section of
`docs/coding-standards/level2-python-instructions.md`). `main()` is the thin
entrypoint that `ps_cli/__main__.py` imports and calls. Parsing lives in
`modules.parser`, handlers and dispatch in `modules.handlers`, and this module is
pure orchestration.

Deviation from L2's literal `## ps-cli` wording: L2 has `main()` itself parse
arguments, dispatch and hold the one `try/except PsCliError`. Here that logic lives
in `run(argv, *, client=None) -> int` and `main()` is reduced to
`sys.exit(run(sys.argv[1:]))`. The split is required for constructor-injection
testability (L1 Dependency Inversion; L2 Common: take dependencies as constructor
or function arguments) and changes no externally observable behavior: `sys.exit`
is still only called in `main()`.

Second deviation: the `--version` handler, `_report_service_version()`, has its own
narrowly scoped `try/except PsCliError` around the service-version lookup only.
`--version` must exit 0 even when PS Service is unreachable or configuration
resolution fails, whereas `run()`'s catch site always returns 1; the two contracts
cannot share one catch site without threading an exit-code override through every
other command's dispatch path. This module therefore has two `try/except
PsCliError` sites: `run()` for real subcommand dispatch and
`_report_service_version()`.
"""

from __future__ import annotations

import sys
import traceback
from importlib.metadata import version as installed_version
from typing import TYPE_CHECKING, cast

from ps_cli.config import load_config
from ps_cli.credentials import build_credential_store
from ps_cli.errors import PsCliError
from ps_cli.http_client import PsServiceClient
from ps_cli.modules.auth_handlers import AUTH_DISPATCH
from ps_cli.modules.config_handlers import CONFIG_DISPATCH
from ps_cli.modules.handlers import DISPATCH, NO_CLIENT_DISPATCH
from ps_cli.modules.parser import build_parser
from ps_cli.targets import resolve_auth_override, resolve_config_dir

if TYPE_CHECKING:
    import argparse
    from collections.abc import Sequence

    from ps_cli.http_client import PsServiceClientProtocol


def _resolve_client(
    args: argparse.Namespace, client: PsServiceClientProtocol | None
) -> PsServiceClientProtocol:
    """Return the client to use for this command: the injected one, else a real `PsServiceClient`.

    Only business commands reach this function; `config` and `auth` commands never
    build a client. The real client is configured from `load_config()`, using the
    `--context` value for this invocation (absent when the flag was never given),
    and carries the credential store, the context name and any per-context auth
    override so that every authenticated call attaches the bearer token stored by
    `ps-cli auth login`. The same `resolve_auth_override` is used by the `auth`
    subcommands.

    Args:
        args: Parsed arguments; only the optional `context` attribute is read.
        client: A pre-built client to use as is, or `None` to build a real one.

    Returns:
        `client` when it is not `None`, otherwise a newly built `PsServiceClient`.

    Raises:
        PsCliError: Configuration or targets resolution fails (raised by
            `load_config()`, `resolve_config_dir()` or `resolve_auth_override()`);
            nothing is raised when `client` is injected.

    Side Effects:
        Reads the configuration directory and credential store when building a
        real client; constructing `PsServiceClient` may print an insecure-URL
        warning to stderr.
    """
    if client is not None:
        return client
    context = getattr(args, "context", None)
    config_dir = resolve_config_dir()
    config = load_config(context=context, config_dir=config_dir)
    credential_store = build_credential_store()
    auth_override = resolve_auth_override(config, config_dir)
    return PsServiceClient(
        config.service_url,
        credential_store=credential_store,
        context=config.context_name,
        auth_override=auth_override,
    )


def _report_service_version(
    args: argparse.Namespace, client: PsServiceClientProtocol | None
) -> None:
    """Print the client version, then the service's version or why it is unavailable.

    The client is resolved through `_resolve_client`, so `--context` applies for this
    invocation only, like every other command. Resolving the client and reading the
    service version are the only two steps that can raise `PsCliError`; both are
    caught here so `--version` always exits 0, printing the error's `msg` (not
    `str(error)`, which adds the decoration used for normal error output). This is
    the second narrowly scoped `try/except PsCliError` described in the module
    docstring. When the versions differ, a single plain `warning: ...` line goes to
    stderr (not the error shape, since this is not an error); there is no comparison
    when the service version is unavailable.

    Args:
        args: Parsed arguments; the optional `context` attribute selects the target.
        client: A pre-built client to query, or `None` to build a real one.

    Returns:
        None. The result is reported on stdout and stderr.

    Side Effects:
        Writes the client and service versions to stdout and, on a version
        mismatch, a warning to stderr. May read configuration and credentials and
        contact PS Service.
    """
    client_version = installed_version("ps-cli")
    print(f"PS-CLI Client Version: {client_version}")
    try:
        active_client = _resolve_client(args, client)
        service_version = active_client.get_service_version()
    except PsCliError as error:
        print(f"PS-Service Version: unavailable ({error.msg})")
        return
    print(f"PS-Service Version: {service_version}")
    if service_version != client_version:
        print(
            f"warning: ps-cli client version ({client_version}) does not match "
            f"ps-service version ({service_version})",
            file=sys.stderr,
        )


def _dispatch_command(
    command: str, args: argparse.Namespace, client: PsServiceClientProtocol | None
) -> None:
    """Route `command` to the matching dispatch table, resolving a client only if needed.

    Four mutually exclusive routes, in priority order:

    - `CONFIG_DISPATCH`: `config` subcommands manage `targets.toml` and
      `credentials.toml` only. They must never construct a `PsServiceClient` or call
      `load_config()`: loading can legitimately fail on a broken `targets.toml`, which
      must not block the commands an operator uses to fix it, and the client's
      constructor has an insecure-URL stderr side effect that makes no sense for a
      command that never contacts PS Service.
    - `AUTH_DISPATCH`: `auth` subcommands never go through `_resolve_client` either,
      because a real client requires an already-valid token for every business call,
      which would make `auth login` circular.
    - `NO_CLIENT_DISPATCH`: currently empty; a standing extension point for a future
      command that must never construct a `PsServiceClient`.
    - `DISPATCH`: every other command, with `client` resolved by `_resolve_client`.

    Split out of `run()` to keep its cyclomatic complexity under the L1 cap of 8.

    Args:
        command: The dispatch key set by the leaf parser's `command` default.
        args: Parsed arguments passed through to the handler.
        client: A pre-built client for business commands, or `None` to build one.

    Returns:
        None. The handler's work is its side effect.

    Raises:
        PsCliError: Propagated from client resolution or from the handler.
        KeyError: `command` is not in any dispatch table (a wiring bug).

    Side Effects:
        Whatever the selected handler does: file, network and console I/O.
    """
    if command in CONFIG_DISPATCH:
        CONFIG_DISPATCH[command](args)
    elif command in AUTH_DISPATCH:
        AUTH_DISPATCH[command](args)
    elif command in NO_CLIENT_DISPATCH:
        NO_CLIENT_DISPATCH[command](args)
    else:
        active_client = _resolve_client(args, client)
        handler = DISPATCH[command]
        handler(args, active_client)


def _print_ps_cli_error(args: argparse.Namespace, error: PsCliError) -> None:
    """Print `error` to stderr; under `-v`, also print its innermost frame's `file:line`.

    Split out of `run()` to keep its cyclomatic complexity under the L1 cap of 8.

    Args:
        args: Parsed arguments; the optional `verbose` attribute enables the
            failure-site line.
        error: The error to report, formatted through `str(error)`.

    Returns:
        None.

    Side Effects:
        Writes to stderr: the formatted error and, under `-v`, a `file:line` pointer
        to the innermost traceback frame.
    """
    print(str(error), file=sys.stderr)
    # `getattr(..., False)`, not `args.verbose`: the shared `-v` action uses
    # `default=SUPPRESS` (see `modules.parser.build_parser`'s docstring), so the
    # attribute is simply absent, not `False`, when `-v` was never given.
    if getattr(args, "verbose", False):
        tb = error.__traceback__
        while tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        if tb is not None:
            print(f"🔦 @ {tb.tb_frame.f_code.co_filename}:{tb.tb_lineno}", file=sys.stderr)


def _print_unexpected_exception(args: argparse.Namespace, exc: Exception) -> None:
    """Print a one-line `{type}: {message}` summary of `exc` to stderr.

    Under `-v`, also print the full traceback: unlike the single-frame pointer of
    `_print_ps_cli_error`, a genuine unhandled bug needs the whole call chain to be
    diagnosed.

    Args:
        args: Parsed arguments; the optional `verbose` attribute enables the
            traceback.
        exc: The unhandled exception being reported. It must be the exception
            currently being handled, because the traceback is read from the
            active exception context.

    Returns:
        None.

    Side Effects:
        Writes to stderr: the summary line and, under `-v`, the full traceback.
    """
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    if getattr(args, "verbose", False):
        traceback.print_exc(file=sys.stderr)


def run(argv: Sequence[str], *, client: PsServiceClientProtocol | None = None) -> int:
    """Parse `argv`, dispatch to the matching handler, and map failures to an exit code.

    Bare `ps-cli` prints help and exits 0; `--version` reports versions and exits 0
    without reaching the `try/except` below (see `_report_service_version`).

    Args:
        argv: Command-line arguments without the program name, e.g.
            `sys.argv[1:]`.
        client: Constructor-injection seam: a pre-built client to use, typed
            against `PsServiceClientProtocol` so a hand-written fake satisfies it
            structurally. When omitted, a real `PsServiceClient` is built from
            `ps_cli.config.load_config()`.

    Returns:
        The process exit code: `0` on success, on `--version` and on the no-command
        help; `1` on a `PsCliError` from subcommand dispatch (its `msg` plus `hint`,
        if present, on stderr, and under `-v` the failure site, without a full
        traceback); `130` on `KeyboardInterrupt` (the operator cancelled, e.g.
        Ctrl-C during the `auth login` device-flow wait); `2` on any other
        `Exception` (an unhandled bug: one `{type}: {message}` line on stderr, the
        full traceback under `-v`).

    Raises:
        SystemExit: Raised by argparse for `--help` and for a malformed argument
            value (exit status 2); it is not caught here and propagates.

    Side Effects:
        Writes to stdout and stderr, and performs whatever file and network I/O the
        selected handler does.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        _report_service_version(args, client)
        return 0
    if args.group is None:
        parser.print_help()
        return 0

    # `set_defaults(command=...)` is the only source of `.command`; argparse's
    # `Namespace` types every attribute as `Any`, so narrow it explicitly here.
    command = cast("str", args.command)

    try:
        _dispatch_command(command, args, client)
    except PsCliError as error:
        _print_ps_cli_error(args, error)
        return 1
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 -- deliberate catch-all boundary: every failure maps to an exit code
        _print_unexpected_exception(args, exc)
        return 2
    return 0


def main() -> None:
    """Run ps-cli with the process arguments and exit with the resulting status.

    The one function `ps_cli/__main__.py` imports and calls; it is deliberately
    thin so that all logic stays testable through `run()`.

    Returns:
        Never returns normally: it calls `sys.exit` with the exit code from `run()`.

    Raises:
        SystemExit: Always, carrying the exit code from `run()`.

    Side Effects:
        Terminates the process; see `run()` for the I/O performed first.
    """
    sys.exit(run(sys.argv[1:]))
