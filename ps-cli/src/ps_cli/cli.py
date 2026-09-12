"""ps-cli's command-line entry point: argument parsing, dispatch, and the error boundary.

``run()`` is the testable core: it parses ``argv``, builds a ``PsServiceClient``
from ``ps_cli.config.load_config()`` only when one is not injected, dispatches to
the matching handler via ``ps_cli.modules.handlers.DISPATCH``, and is the main
``try/except PsCliError`` in the call chain -- catching every error from a real
subcommand dispatch and returning exit code 1. ``main()`` is the literal, thin
entrypoint ``ps_cli/__main__.py`` imports and calls (AC-BI-005). Mirrors gh-tt's
`gh_tt.py`: parsing lives in `modules.parser`, handlers and dispatch live in
`modules.handlers`, and this module is pure orchestration.

**Flagged, orchestrator-accepted deviation from L2's literal `## ps-cli` wording**
(PLAN.md §1 D9): L2 states that `main()` itself parses args, dispatches, and
contains the one `try`/`except PsCliError`. This module instead puts that logic in
`run(argv, *, client=None) -> int`, with `main()` reduced to
`sys.exit(run(sys.argv[1:]))`. This split is required for constructor-injection
testability (L1 Dependency Inversion; L2 Common's "no DI framework... take
dependencies as constructor/function arguments") without changing any externally
observable behavior -- `sys.exit` still only happens in `main()`.

**Second flagged deviation (issue #82 PLAN.md §1 D6):** `--version`'s handler,
`_report_service_version()`, has its own second, narrowly-scoped
`try/except PsCliError` around only its service-version lookup. AC-BI-008 requires
`--version` to exit 0 even when PS Service is unreachable or config resolution
fails, whereas `run()`'s own catch site below always returns 1 -- the two contracts
cannot share one catch site without threading an exit-code override through every
other command's dispatch path, which this plan rejects as unnecessarily invasive.
This module therefore has **two** `try/except PsCliError` sites, not one: this
one (`run()`, for real subcommand dispatch) and `_report_service_version()`'s own.
"""

from __future__ import annotations

import sys
from importlib.metadata import version as installed_version
from typing import TYPE_CHECKING, cast

from ps_cli.config import load_config
from ps_cli.errors import PsCliError
from ps_cli.http_client import PsServiceClient
from ps_cli.modules.config_handlers import CONFIG_DISPATCH
from ps_cli.modules.handlers import DISPATCH, NO_CLIENT_DISPATCH
from ps_cli.modules.parser import build_parser

if TYPE_CHECKING:
    import argparse
    from collections.abc import Sequence

    from ps_cli.http_client import PsServiceClientProtocol


def _resolve_client(
    args: argparse.Namespace, client: PsServiceClientProtocol | None
) -> PsServiceClientProtocol:
    """Return `client` unchanged if injected, else build a real `PsServiceClient`.

    Only reached for non-`config` commands (PLAN.md issue #56 §1 D8) -- `load_config()` is
    never called otherwise. `args.context` (the `--context` flag's resolved value for this
    invocation, absent via `SUPPRESS` when never given) feeds `load_config()`'s `context`
    param, PLAN.md D3's case 2.
    """
    if client is not None:
        return client
    context = getattr(args, "context", None)
    return PsServiceClient(load_config(context=context).service_url)


def _report_service_version(
    args: argparse.Namespace, client: PsServiceClientProtocol | None
) -> None:
    """Print the client version, then the resolved service's version or why it's unavailable.

    AC-BI-005/006/007: resolves the client via the existing `_resolve_client` (so
    `--context` applies for this invocation only, exactly like every other command).
    AC-BI-008: `_resolve_client`'s `load_config()` call and `get_service_version()` are
    the only two calls here that can raise `PsCliError` -- both are caught locally so
    `--version` always exits 0 even on failure, printing the resolved `.msg` (never
    `str(error)`, which would add the "❌"/"💡" decoration -- see `errors.py`). This is a
    **second, narrowly-scoped `try/except PsCliError`**, a flagged deviation from this
    module's own stated "sole try/except PsCliError in the call chain" (see module
    docstring and `run()`'s own docstring): AC-BI-008 requires exit 0 here, whereas
    `run()`'s one general catch site always returns 1 -- the two contracts cannot share
    one site without threading an exit-code override through every other command's
    dispatch path, which this plan rejects as unnecessarily invasive (mirrors this
    file's own precedent for a flagged, orchestrator-accepted deviation, see the module
    docstring's `main()`/`run()` split).
    AC-BI-009: a version mismatch is reported as a single `warning: ...` line on stderr,
    mirroring `http_client.py`'s own existing insecure-URL warning convention -- plain
    lowercase `warning: ` prefix, not the `PsCliError` "❌"/"💡" shape, since this is not
    an error (exit stays 0). Only reachable from the success path, never the "unavailable"
    branch above (there is no service version to compare against on failure).
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

    Three mutually exclusive routes, in priority order: `CONFIG_DISPATCH`
    (`config` subcommands manage `targets.toml`/`credentials.toml` only --
    they must never construct a `PsServiceClient` or call `load_config()`,
    PLAN.md issue #56 §1 D8, critical: `load_config()` can legitimately raise
    on a broken `targets.toml`, which must not block the very commands an
    operator would use to fix it, and `PsServiceClient`'s constructor has an
    "insecure URL" stderr side effect that makes no sense for a command that
    never contacts PS Service); `NO_CLIENT_DISPATCH` (`catalog_list`, issue
    #66 D13, reads the local curated-content repo only -- like `config_*`
    above it must never construct a `PsServiceClient`, but unlike `config_*`
    its own dispatch adapter still resolves `curated_repo_path` via
    `load_config()` internally; `.service_url` is simply never touched);
    otherwise `DISPATCH`, resolving `client` via `_resolve_client` first.
    Extracted out of `run()` to keep its own cyclomatic complexity under
    `level1-coding-principles.md`'s cap of 8.
    """
    if command in CONFIG_DISPATCH:
        CONFIG_DISPATCH[command](args)
    elif command in NO_CLIENT_DISPATCH:
        NO_CLIENT_DISPATCH[command](args)
    else:
        active_client = _resolve_client(args, client)
        handler = DISPATCH[command]
        handler(args, active_client)


def run(argv: Sequence[str], *, client: PsServiceClientProtocol | None = None) -> int:
    """Parse `argv`, dispatch to the matching handler, catch `PsCliError` once here.

    Returns the process exit code: `0` on success or on `--version`/no-command
    help (both mirror gh-tt: bare `ps-cli` prints help and exits 0, matching
    gh-tt's own no-command behavior), `1` on a `PsCliError` from real subcommand
    dispatch (formatted as `msg` plus `hint`, if present, to stderr -- plus a
    `-v`/`--verbose` failure-site line, no full traceback). Any other exception
    is a bug, not a user error, and propagates uncaught (L2 ps-cli "Let bugs
    crash"); a malformed argument value (e.g. a badly-shaped `celex`) is caught
    by argparse itself during `parse_args()` below and exits 2 via `SystemExit`,
    not through this function's own return value (PLAN.md §1 D10).

    `--version` is the one exception to "catch `PsCliError` once here": it never
    reaches this function's `try/except` below at all -- `_report_service_version()`
    (issue #82 PLAN.md §1 D6, see module docstring's second flagged deviation) has
    its own local `try/except PsCliError` so it can always return exit 0
    (AC-BI-008), even when this function's own catch site would have returned 1.

    `client` is the constructor-injection seam: when omitted, a real
    `PsServiceClient` is built from `ps_cli.config.load_config()`. Typed
    against `PsServiceClientProtocol`, not the concrete `PsServiceClient`
    class, so a test's hand-written fake satisfies it structurally --
    matching L2 Common's "Use Protocol for interfaces" (PLAN.md §1 D10).
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
        return 1
    return 0


def main() -> None:
    """The one function `ps_cli/__main__.py` imports and calls (AC-BI-005)."""
    sys.exit(run(sys.argv[1:]))
