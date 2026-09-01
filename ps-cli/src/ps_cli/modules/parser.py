"""ps-cli's argparse definition: `build_parser()`.

Mirrors gh-tt's `modules/tt_parser.py` — this module only builds the parser; it
never calls `parse_args()` itself (that's `ps_cli.cli.run()`'s job).
"""

from __future__ import annotations

import argparse
import re

# Mirrors ps_service/api/models.py's CatalogIngestionRequest.celex
# Field(pattern=r"^3\d{4}[A-Z]\d{4}$") verbatim -- vendored per L2 Project
# Structure's "fully decoupled... vendors its own copy" rule, NOT imported.
# Must be updated in lockstep if the server's pattern ever changes.
_CELEX_PATTERN = re.compile(r"^3\d{4}[A-Z]\d{4}$")


def _celex_type(value: str) -> str:
    """`type=` callback for the `celex` positional: format-validate at parse time.

    Mirrors gh-tt's `tt_parser.py::valid_status_states` pattern -- a single
    argument's own value format is checked via `type=`, raising
    `argparse.ArgumentTypeError` (argparse turns this into a normal usage
    error, exit code 2), not via a post-parse `assert_contract`/`PsCliError`
    check in the handler. By the time a handler runs, `celex` is guaranteed
    well-formed.
    """
    trimmed_value = value.strip()
    if not _CELEX_PATTERN.fullmatch(trimmed_value):
        msg = (
            f"'{trimmed_value}' is not a 10-character CELEX identifier "
            "(expected: 3<4 digits><1 uppercase letter><4 digits>, e.g. 32016R0679)"
        )
        raise argparse.ArgumentTypeError(msg)
    return trimmed_value


def _fixture_path_type(value: str) -> str:
    """`type=` callback for the `fixture_path` positional: format-validate at parse time.

    Same rationale as `_celex_type` -- format validation belongs at parse
    time via `type=`, not in the handler.
    """
    if not value or not value.endswith(".json"):
        msg = f"'{value}' must be a non-empty path ending in '.json'"
        raise argparse.ArgumentTypeError(msg)
    return value


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser: a `regulations` subcommand group with `list`/`ingest` leaves.

    Each leaf subparser sets `command` via `set_defaults()` (L2's prescribed
    "values derived at parse time" pattern) to the key `run()` looks up in
    `DISPATCH`. `regulations_command`/`internal_command` are `required=True`,
    so once a `group` is chosen, a successful `parse_args()` call always
    yields a `command` for it -- `run()` never has to guard against a missing
    one at that level. The top-level `group` itself is not required (an
    operator can run `ps-cli` bare, or `ps-cli --version`, with no subcommand
    at all -- `run()` handles both before dispatch).

    `-v`/`--verbose` is a shared flag (L2 `## ps-cli` "parent parsers for
    flags shared across subcommands") available before or after any
    subcommand, mirroring gh-tt's `parent_parser` reuse. `default=SUPPRESS`
    on the shared action is deliberate, not incidental: `argparse`'s
    subparser dispatch parses each subcommand into a *fresh* namespace and
    then unconditionally copies every one of its attributes onto the parent
    namespace (see `argparse.py::_SubParsersAction.__call__`) -- without
    `SUPPRESS`, a leaf subparser's own unset `-v` (default `False`) would
    silently clobber a `-v` already given before the subcommand name. `run()`
    reads this via `getattr(args, "verbose", False)`, never `args.verbose`
    directly, since no level of the parser setting it leaves the attribute
    absent rather than `False`.
    """
    verbose_parent_parser = argparse.ArgumentParser(add_help=False)
    verbose_parent_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print the failure site on error.",
    )

    parser = argparse.ArgumentParser(
        prog="ps-cli",
        description="Policy System CLI: operator client for PS Service's REST API.",
        parents=[verbose_parent_parser],
    )
    parser.add_argument(
        "--version", action="store_true", help="Print version information and exit."
    )
    top_level_subparsers = parser.add_subparsers(dest="group", required=False)

    regulations_parser = top_level_subparsers.add_parser(
        "regulations",
        parents=[verbose_parent_parser],
        help="Commands for the regulation catalog.",
    )
    regulations_subparsers = regulations_parser.add_subparsers(
        dest="regulations_command", required=True
    )
    list_parser = regulations_subparsers.add_parser(
        "list",
        parents=[verbose_parent_parser],
        help="List the curated regulation catalog (CELEX + title).",
    )
    list_parser.set_defaults(command="regulations_list")

    ingest_parser = regulations_subparsers.add_parser(
        "ingest",
        parents=[verbose_parent_parser],
        help="Ingest a curated EU regulation by its CELEX identifier.",
    )
    ingest_parser.add_argument(
        "celex",
        type=_celex_type,
        help="The regulation's 10-character CELEX identifier.",
    )
    ingest_parser.set_defaults(command="regulations_ingest")

    internal_parser = top_level_subparsers.add_parser(
        "internal",
        parents=[verbose_parent_parser],
        help="Commands for internal-document ingestion.",
    )
    internal_subparsers = internal_parser.add_subparsers(dest="internal_command", required=True)
    internal_ingest_parser = internal_subparsers.add_parser(
        "ingest",
        parents=[verbose_parent_parser],
        help=(
            "Ingest an internal-document fixture by path. The path is resolved on PS "
            "Service's own fixtures root, not read from your local machine."
        ),
    )
    internal_ingest_parser.add_argument(
        "fixture_path",
        type=_fixture_path_type,
        help="Path to the fixture .json file, relative to PS Service's fixtures root.",
    )
    internal_ingest_parser.set_defaults(command="internal_ingest")

    return parser
