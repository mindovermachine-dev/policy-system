"""ps-cli's command-line entry point: argument parsing, dispatch, and the error boundary.

``run()`` is the testable core: it parses ``argv``, builds a ``PsServiceClient``
from ``ps_cli.config.load_config()`` only when one is not injected, dispatches to
the matching handler via ``ps_cli.modules.handlers.DISPATCH``, and is the sole
``try/except PsCliError`` in the call chain. ``main()`` is the literal, thin
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
observable behavior -- there is still exactly one `try/except PsCliError` in the
call chain, and `sys.exit` still only happens in `main()`.
"""

from __future__ import annotations

import sys
from importlib.metadata import version as installed_version
from typing import TYPE_CHECKING, cast

from ps_cli.config import load_config
from ps_cli.errors import PsCliError
from ps_cli.http_client import PsServiceClient
from ps_cli.modules.handlers import DISPATCH
from ps_cli.modules.parser import build_parser

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ps_cli.http_client import PsServiceClientProtocol


def run(argv: Sequence[str], *, client: PsServiceClientProtocol | None = None) -> int:
    """Parse `argv`, dispatch to the matching handler, catch `PsCliError` once.

    Returns the process exit code: `0` on success or on `--version`/no-command
    help (both mirror gh-tt: bare `ps-cli` prints help and exits 0, matching
    gh-tt's own no-command behavior), `1` on a `PsCliError` (formatted as
    `msg` plus `hint`, if present, to stderr -- plus a `-v`/`--verbose`
    failure-site line, no full traceback). Any other exception is a bug, not
    a user error, and propagates uncaught (L2 ps-cli "Let bugs crash"); a
    malformed argument value (e.g. a badly-shaped `celex`) is caught by
    argparse itself during `parse_args()` below and exits 2 via `SystemExit`,
    not through this function's own return value (PLAN.md §1 D10).

    `client` is the constructor-injection seam: when omitted, a real
    `PsServiceClient` is built from `ps_cli.config.load_config()`. Typed
    against `PsServiceClientProtocol`, not the concrete `PsServiceClient`
    class, so a test's hand-written fake satisfies it structurally --
    matching L2 Common's "Use Protocol for interfaces" (PLAN.md §1 D10).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(installed_version("ps-cli"))
        return 0
    if args.group is None:
        parser.print_help()
        return 0

    # `set_defaults(command=...)` is the only source of `.command`; argparse's
    # `Namespace` types every attribute as `Any`, so narrow it explicitly here.
    command = cast("str", args.command)

    try:
        active_client = client if client is not None else PsServiceClient(load_config().service_url)
        handler = DISPATCH[command]
        handler(args, active_client)
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
