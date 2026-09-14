"""ps-cli's argparse definition: `build_parser()`.

Mirrors gh-tt's `modules/tt_parser.py` — this module only builds the parser; it
never calls `parse_args()` itself (that's `ps_cli.cli.run()`'s job).
"""

from __future__ import annotations

import argparse
import re

from ps_cli.config import is_valid_service_url

# Mirrors ps_service/api/models.py's CatalogIngestionRequest.celex
# Field(pattern=r"^3\d{4}[A-Z]\d{4}$") verbatim -- vendored per L2 Project
# Structure's "fully decoupled... vendors its own copy" rule, NOT imported.
# Must be updated in lockstep if the server's pattern ever changes.
_CELEX_PATTERN = re.compile(r"^3\d{4}[A-Z]\d{4}$")

# A context name is always a safe bare TOML key by construction (alnum start,
# alnum/`_`/`-` body, 1-64 chars) -- no quoting logic needed by `toml_writer`.
# Not derived from any cited doc; a documented assumption, see PLAN.md (issue
# #56) §1 D6 / §5 Risk 2.
_CONTEXT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

# A curated instrument id (e.g. "CRA-1.0", `curated-content/catalog.json`'s real entries,
# D1) is not constrained by any existing server-side pattern -- `RestorationManifestPayload
# .instrument_id`/`CatalogInstrumentEntry.instrument_id` (ps_service/api/models.py) are both
# bare `Field(min_length=1)`, no `pattern=`. This charset is instead this module's own
# documented assumption, chosen because `restore instrument <instrument_id>` later uses the
# value to build a local filesystem path (`catalog_repo.read_artifact`, Slice 7.1) -- alnum
# start, alnum/`_`/`-`/`.` body, no `/`, matching every real `catalog.json` entry
# (`CRA-1.0`, `GDPR-1.0`, `NIS2-1.0`) while rejecting a path-traversal payload at parse
# time (L1 "Fail Fast at Boundaries" -- the first of two defense-in-depth layers for this
# security-critical filesystem-path sink; `read_artifact`'s own `instrument_dir.is_dir()`
# check is the second).
_INSTRUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


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


def _context_name_type(value: str) -> str:
    """`type=` callback for a context-name positional (`set-context`/`use-context`).

    Format-validates against PLAN.md (issue #56) §1 D6's charset: alnum start,
    alnum/`_`/`-` body, 1-64 characters total. Same `type=`-callback convention
    as `_celex_type` -- raises `argparse.ArgumentTypeError` (exit 2), never a
    post-parse `assert_contract`/`PsCliError` check.
    """
    if not _CONTEXT_NAME_PATTERN.fullmatch(value):
        msg = (
            f"'{value}' is not a valid context name (expected: 1-64 characters, "
            "starting with a letter or digit, followed by letters, digits, '_', or '-')"
        )
        raise argparse.ArgumentTypeError(msg)
    return value


def _service_url_type(value: str) -> str:
    """`type=` callback for `--url` (`config set-context`): format-validate at parse time.

    Reuses `ps_cli.config.is_valid_service_url()` (PLAN.md (issue #56) §1 D5)
    rather than a second `urlparse` check of its own, per L1 DRY (`level1-
    coding-principles.md:55-58`).
    """
    if not is_valid_service_url(value):
        msg = f"'{value}' is not a valid http(s) URL"
        raise argparse.ArgumentTypeError(msg)
    return value


def _instrument_id_type(value: str) -> str:
    """`type=` callback for the `instrument_id` positional (`restore instrument`).

    Format-validates at parse time -- same `type=`-callback convention as
    `_celex_type`. Rejects anything outside `_INSTRUMENT_ID_PATTERN`'s
    charset (which already excludes `/`); the redundant explicit `..`
    substring check below is defense-in-depth documentation, not load-
    bearing on its own -- see `_INSTRUMENT_ID_PATTERN`'s own comment for why
    this matters (the value later builds a local filesystem path).
    """
    if not _INSTRUMENT_ID_PATTERN.fullmatch(value) or ".." in value:
        msg = (
            f"'{value}' is not a valid instrument id (expected: alphanumeric, "
            "starting with a letter or digit, followed by letters, digits, "
            "'_', '-', or '.', e.g. 'CRA-1.0')"
        )
        raise argparse.ArgumentTypeError(msg)
    return value


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
    """Build the top-level parser: a strict kubectl-style `<verb> <resource>` surface.

    Every top-level group except `config` is a verb (`get`, `ingest`, `restore`,
    `check`); each verb group is itself an `add_subparsers()` group whose leaves are
    resource nouns (e.g. `ingest regulation`, `restore instrument`) -- `ps-cli <verb>
    <resource> [name] [flags]`, modeled on kubectl. `config` is the one exception
    group, mirroring kubectl's own `kubectl config` subcommands: its leaves are
    verb-noun compounds (`set-context`/`use-context`/`get-contexts`) rather than
    nested `config <verb> <resource>`.

    Each leaf subparser sets `command` via `set_defaults()` (L2's prescribed
    "values derived at parse time" pattern) to the key `run()` looks up in
    `DISPATCH`. Every verb group's own `<verb>_command` subparsers dest (e.g.
    `get_command`, `ingest_command`) is `required=True`, so once a `group` is
    chosen, a successful `parse_args()` call always yields a `command` for it --
    `run()` never has to guard against a missing one at that level. The top-level
    `group` itself is not required (an operator can run `ps-cli` bare, or `ps-cli
    --version`, with no subcommand at all -- `run()` handles both before dispatch).

    `-v`/`--verbose` and `--context <name>` (issue #56, PLAN.md §1 D7) are shared flags
    (L2 `## ps-cli` "parent parsers for flags shared across subcommands") available
    before or after any subcommand, mirroring gh-tt's `parent_parser` reuse.
    `default=SUPPRESS` on both shared actions is deliberate, not incidental: `argparse`'s
    subparser dispatch parses each subcommand into a *fresh* namespace and then
    unconditionally copies every one of its attributes onto the parent namespace (see
    `argparse.py::_SubParsersAction.__call__`) -- without `SUPPRESS`, a leaf subparser's
    own unset `-v`/`--context` (default `False`/`None`) would silently clobber a value
    already given before the subcommand name. `run()` reads these via
    `getattr(args, "verbose", False)`/`getattr(args, "context", None)`, never
    `args.verbose`/`args.context` directly, since no level of the parser setting them
    leaves the attribute absent rather than its default.
    """
    verbose_parent_parser = argparse.ArgumentParser(add_help=False)
    verbose_parent_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print the failure site on error.",
    )
    verbose_parent_parser.add_argument(
        "--context",
        default=argparse.SUPPRESS,
        type=_context_name_type,
        help=(
            "Use this named context's PS Service URL for this invocation only "
            "(overrides targets.toml's current_context; never persisted). "
            "See PLAN.md (issue #56) §1 D3/D7."
        ),
    )

    parser = argparse.ArgumentParser(
        prog="ps-cli",
        description="Policy System CLI: operator client for PS Service's REST API.",
        parents=[verbose_parent_parser],
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print PS-CLI client and PS Service versions and exit.",
    )
    top_level_subparsers = parser.add_subparsers(dest="group", required=False)

    # `get` (read-only lookups): service health, or the local curated catalog. Neither
    # leaf mutates any state -- `get health` reports PS Service's reachability/health/
    # readiness, `get catalog` reads `curated_repo_path`'s on-disk `catalog.json`
    # directly and never contacts PS Service at all (D13: it skips
    # `_resolve_client`/`load_config().service_url` entirely, like `config`'s commands).
    get_parser = top_level_subparsers.add_parser(
        "get",
        parents=[verbose_parent_parser],
        help="Read-only lookups: service health, or the local curated catalog.",
    )
    get_subparsers = get_parser.add_subparsers(dest="get_command", required=True)

    get_health_parser = get_subparsers.add_parser(
        "health",
        parents=[verbose_parent_parser],
        help="Report PS Service's reachability, health (`/health`), and readiness (`/ready`).",
    )
    get_health_parser.set_defaults(command="get_health")

    get_catalog_parser = get_subparsers.add_parser(
        "catalog",
        parents=[verbose_parent_parser],
        help=(
            "List every curated instrument in the local curated-content repo "
            "(no PS Service connection needed)."
        ),
    )
    get_catalog_parser.set_defaults(command="get_catalog")

    # `ingest` (bring new content into PS Service): a curated EU regulation by CELEX,
    # or an internal-document fixture by path.
    ingest_parser = top_level_subparsers.add_parser(
        "ingest",
        parents=[verbose_parent_parser],
        help="Bring new content into PS Service: a curated regulation or an internal document.",
    )
    ingest_subparsers = ingest_parser.add_subparsers(dest="ingest_command", required=True)

    ingest_regulation_parser = ingest_subparsers.add_parser(
        "regulation",
        parents=[verbose_parent_parser],
        help="Ingest a curated EU regulation by its CELEX identifier.",
    )
    ingest_regulation_parser.add_argument(
        "celex",
        type=_celex_type,
        help="The regulation's 10-character CELEX identifier.",
    )
    ingest_regulation_parser.set_defaults(command="ingest_regulation")

    ingest_document_parser = ingest_subparsers.add_parser(
        "document",
        parents=[verbose_parent_parser],
        help=(
            "Ingest an internal-document fixture by path. The path is resolved on PS "
            "Service's own fixtures root, not read from your local machine."
        ),
    )
    ingest_document_parser.add_argument(
        "fixture_path",
        type=_fixture_path_type,
        help="Path to the fixture .json file, relative to PS Service's fixtures root.",
    )
    ingest_document_parser.set_defaults(command="ingest_document")

    # `config` subcommand group (issue #56, PLAN.md §1 D7): manages named
    # PS Service targets (contexts) in `targets.toml`. `set-context` (Slice 14),
    # `use-context` (Slice 25), and `get-contexts` (Slice 28) are all wired here.
    # This is the one exception to the verb-then-resource shape every other group
    # follows -- it mirrors kubectl's own `kubectl config` subcommands, whose leaves
    # are verb-noun compounds (`set-context`/`use-context`/`get-contexts`), not
    # nested `config <verb> <resource>`.
    config_parser = top_level_subparsers.add_parser(
        "config",
        parents=[verbose_parent_parser],
        help="Manage named PS Service targets (contexts) -- kubectl-style config verbs.",
    )
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)

    set_context_parser = config_subparsers.add_parser(
        "set-context",
        parents=[verbose_parent_parser],
        help="Create or update a named context's PS Service URL.",
    )
    set_context_parser.add_argument(
        "name",
        type=_context_name_type,
        help="The context name.",
    )
    set_context_parser.add_argument(
        "--url",
        required=True,
        type=_service_url_type,
        help="The PS Service URL for this context.",
    )
    set_context_parser.set_defaults(command="config_set_context")

    use_context_parser = config_subparsers.add_parser(
        "use-context",
        parents=[verbose_parent_parser],
        help="Select the named context used by every subsequent command.",
    )
    use_context_parser.add_argument(
        "name",
        type=_context_name_type,
        help="The context name to select.",
    )
    use_context_parser.set_defaults(command="config_use_context")

    get_contexts_parser = config_subparsers.add_parser(
        "get-contexts",
        parents=[verbose_parent_parser],
        help="List every named context, marking the currently-selected one.",
    )
    get_contexts_parser.set_defaults(command="config_get_contexts")

    # `restore` (restore a curated instrument's artifact into PS Service): reads the
    # artifact off `curated_repo_path` locally and uploads it (`POST /restorations`),
    # reusing `_resolve_client` exactly like `ingest regulation`.
    restore_parser = top_level_subparsers.add_parser(
        "restore",
        parents=[verbose_parent_parser],
        help="Restore a curated instrument's artifact into PS Service.",
    )
    restore_subparsers = restore_parser.add_subparsers(dest="restore_command", required=True)

    restore_instrument_parser = restore_subparsers.add_parser(
        "instrument",
        parents=[verbose_parent_parser],
        help="Restore one curated instrument's artifact into PS Service.",
    )
    restore_instrument_parser.add_argument(
        "instrument_id",
        type=_instrument_id_type,
        help="The curated instrument's id, e.g. 'CRA-1.0'.",
    )
    restore_instrument_parser.set_defaults(command="restore_instrument")

    # `check` (issue #73, PLAN.md §1 D1): sweep for state changes. `regulations` is
    # its only leaf today -- a subparser group of one, for structural consistency
    # with `get`/`ingest`/`restore` (uniformity matters more than terseness here, and
    # it leaves room for a future `check catalog` or similar without another
    # reshuffle) rather than a subgroup-less top-level leaf.
    check_parser = top_level_subparsers.add_parser(
        "check",
        parents=[verbose_parent_parser],
        help="Sweep for state changes against tracked content.",
    )
    check_subparsers = check_parser.add_subparsers(dest="check_command", required=True)

    check_regulations_parser = check_subparsers.add_parser(
        "regulations",
        parents=[verbose_parent_parser],
        help="Sweep every tracked instrument for amendments and re-ingest any found.",
    )
    check_regulations_parser.set_defaults(command="check_regulations")

    return parser
