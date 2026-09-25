"""ps-cli's argparse definition: `build_parser()`.

Mirrors gh-tt's `modules/tt_parser.py` — this module only builds the parser; it
never calls `parse_args()` itself (that's `ps_cli.cli.run()`'s job).
"""

from __future__ import annotations

import argparse
import re

from ps_cli.config import is_valid_service_url

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


def _context_name_type(value: str) -> str:
    """`type=` callback for a context-name positional (`set-context`/`use-context`).

    Format-validates against PLAN.md (issue #56) §1 D6's charset: alnum start,
    alnum/`_`/`-` body, 1-64 characters total. Same `type=`-callback convention
    as `_instrument_id_type` -- raises `argparse.ArgumentTypeError` (exit 2), never a
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


def _auth_scopes_type(value: str) -> tuple[str, ...]:
    """`type=` callback for `--auth-scopes`: a comma-separated string -> `tuple[str, ...]`.

    Chosen over `action="append"` repeated-flag style since `targets.toml`'s own
    `scopes` field (`ps_cli.targets.AuthOverrides.scopes`) is a TOML array either
    way, and a single comma-separated flag reads more naturally next to `--url` on
    one command line (issue #57 Slice 8, flagged as a non-load-bearing UX choice).
    Each comma-separated element is stripped of surrounding whitespace.
    """
    return tuple(item.strip() for item in value.split(","))


def _instrument_id_type(value: str) -> str:
    """`type=` callback for the `instrument_id` positional (`restore instrument`).

    Format-validates at parse time -- same `type=`-callback convention as
    `_context_name_type`. Rejects anything outside `_INSTRUMENT_ID_PATTERN`'s
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


def _document_path_type(value: str) -> str:
    """`type=` callback for the `document_path` positional: format-validate at parse time.

    Same rationale as `_instrument_id_type` -- format validation belongs at parse
    time via `type=`, not in the handler. `value` is a plain local
    filesystem path (relative to the operator's cwd, or absolute), resolved
    the ordinary way -- not resolved against any PS Service or ps-cli-owned
    root (issue #91).
    """
    if not value or not value.endswith(".json"):
        msg = f"'{value}' must be a non-empty path ending in '.json'"
        raise argparse.ArgumentTypeError(msg)
    return value


def _add_export_parser(
    top_level_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]  # argparse: no public alias for add_subparsers()'s return type
    verbose_parent_parser: argparse.ArgumentParser,
) -> None:
    """Add the `export` verb group to `top_level_subparsers` (issue #71).

    Extracted out of `build_parser()` itself (rather than inlined alongside every
    other verb group there) purely to keep that already-large function under
    ruff's `PLR0915` statement-count budget -- no behavioral difference from
    inlining. Mirrors `restore`'s block: `POST /exports` /
    `client.export_instrument`, in the opposite direction. `destination` is a
    second, optional positional (`nargs="?"`, default `None`) rather than a
    `type=` callback -- writability is a runtime filesystem check, not a format
    check, so it belongs in the handler, not here (PLAN.md §1 D8).
    """
    export_parser = top_level_subparsers.add_parser(
        "export",
        parents=[verbose_parent_parser],
        help="Export an already-ingested instrument's artifact from PS Service.",
    )
    export_subparsers = export_parser.add_subparsers(dest="export_command", required=True)

    export_instrument_parser = export_subparsers.add_parser(
        "instrument",
        parents=[verbose_parent_parser],
        help="Export one already-ingested instrument's artifact from PS Service.",
    )
    export_instrument_parser.add_argument(
        "instrument_id",
        type=_instrument_id_type,
        help="The already-ingested instrument's id, e.g. 'CRA-1.0'.",
    )
    export_instrument_parser.add_argument(
        "destination",
        nargs="?",
        default=None,
        help=(
            "Directory to write baseline.json/native.json/manifest.json into "
            "(default: the current working directory)."
        ),
    )
    export_instrument_parser.set_defaults(command="export_instrument")


def _add_config_parser(
    top_level_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]  # argparse: no public alias for add_subparsers()'s return type
    verbose_parent_parser: argparse.ArgumentParser,
) -> None:
    """Add the `config` subcommand group to `top_level_subparsers` (issue #56, PLAN.md §1 D7).

    Manages named PS Service targets (contexts) in `targets.toml`. `set-context`
    (Slice 14; issue #57 Slice 8's `--auth-*` flags), `use-context` (Slice 25), and
    `get-contexts` (Slice 28) are all wired here. This is the one exception to the
    verb-then-resource shape every other group follows -- it mirrors kubectl's own
    `kubectl config` subcommands, whose leaves are verb-noun compounds
    (`set-context`/`use-context`/`get-contexts`), not nested `config <verb>
    <resource>`. Extracted out of `build_parser()` itself purely to keep that
    already-large function under ruff's `PLR0915` statement-count budget -- no
    behavioral difference from inlining, same rationale as `_add_export_parser()`.
    """
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
    _add_set_context_auth_flags(set_context_parser)
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


def _add_set_context_auth_flags(set_context_parser: argparse.ArgumentParser) -> None:
    """Add the four `--auth-*` override flags to `set_context_parser` (issue #57 Slice 8).

    Extracted out of `build_parser()` itself purely to keep that already-large
    function under ruff's `PLR0915` statement-count budget -- no behavioral
    difference from inlining, same rationale as `_add_export_parser()`. Per-context
    OIDC parameter overrides, layered onto device-flow discovery
    (`ps_cli.oidc_discovery.resolve_auth_parameters`). All four default to `None` --
    omitting a flag leaves that field untouched by `handle_config_set_context`'s
    per-field merge, it does not clear it (AC-BI-006).
    """
    set_context_parser.add_argument(
        "--auth-issuer",
        default=None,
        help="Override the discovered OIDC issuer for this context.",
    )
    set_context_parser.add_argument(
        "--auth-client-id",
        default=None,
        help="Override the discovered OIDC client id for this context.",
    )
    set_context_parser.add_argument(
        "--auth-audience",
        default=None,
        help="The OIDC audience to request for this context (no discovered default).",
    )
    set_context_parser.add_argument(
        "--auth-scopes",
        default=None,
        type=_auth_scopes_type,
        help="Comma-separated OIDC scopes to override for this context, e.g. 'openid,profile'.",
    )


def _add_auth_parser(
    top_level_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]  # argparse: no public alias for add_subparsers()'s return type
    verbose_parent_parser: argparse.ArgumentParser,
) -> None:
    """Add the `auth` command group to `top_level_subparsers` (issue #57 Slice 13).

    Mirrors `gh auth login/status/logout` -- L2's own stated precedent for this group
    ("adapted from the proven shape of gh-tt"), not kubectl. `config` is the closest
    existing exception-group precedent in this repo (`_add_config_parser`'s own
    docstring), but `auth`'s three leaves are plain verbs, not verb-noun compounds.
    `login` (Slice 13) takes only the shared `-v`/`--context` parents -- the context to
    authenticate is resolved exactly like every other command (D-57-5). `status`/
    `logout` (Slices 19/20) get their parser leaves now too, since later slices need
    them, but only `login`'s `AUTH_DISPATCH` entry has a real handler this slice --
    see `auth_handlers.py`'s own module docstring for why `status`/`logout` still get
    placeholder dispatch entries rather than being left out of the dict entirely.
    Extracted out of `build_parser()` itself purely to keep that already-large function
    under ruff's `PLR0915` statement-count budget, same rationale as `_add_export_parser`.
    """
    auth_parser = top_level_subparsers.add_parser(
        "auth",
        parents=[verbose_parent_parser],
        help="Manage OIDC device-flow login for the current context.",
    )
    auth_subparsers = auth_parser.add_subparsers(dest="auth_command", required=True)

    login_parser = auth_subparsers.add_parser(
        "login",
        parents=[verbose_parent_parser],
        help="Log in to the current context via OIDC device-flow.",
    )
    login_parser.set_defaults(command="auth_login")

    status_parser = auth_subparsers.add_parser(
        "status",
        parents=[verbose_parent_parser],
        help="Show the current context's login status.",
    )
    status_parser.set_defaults(command="auth_status")

    logout_parser = auth_subparsers.add_parser(
        "logout",
        parents=[verbose_parent_parser],
        help="Log out of the current context.",
    )
    logout_parser.set_defaults(command="auth_logout")


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser: a strict kubectl-style `<verb> <resource>` surface.

    Every top-level group except `config` is a verb (`get`, `ingest`, `restore`);
    each verb group is itself an `add_subparsers()` group whose leaves are
    resource nouns (e.g. `ingest document`, `restore instrument`) -- `ps-cli <verb>
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

    # `ingest` (bring new content into PS Service): an internal-document fixture by
    # path. (A curated EU regulation by CELEX is ingested via the `ps-ingest-regulation`
    # MCP skill instead -- see docs/artifacts/user-guide.md.)
    ingest_parser = top_level_subparsers.add_parser(
        "ingest",
        parents=[verbose_parent_parser],
        help="Bring new content into PS Service: an internal document.",
    )
    ingest_subparsers = ingest_parser.add_subparsers(dest="ingest_command", required=True)

    ingest_document_parser = ingest_subparsers.add_parser(
        "document",
        parents=[verbose_parent_parser],
        help=(
            "Ingest an internal document by local path. The file is read from your "
            "own machine and its content is sent to PS Service."
        ),
    )
    ingest_document_parser.add_argument(
        "document_path",
        type=_document_path_type,
        help="Path to the local .json document to ingest.",
    )
    ingest_document_parser.set_defaults(command="ingest_document")

    # `config` subcommand group (issue #56, PLAN.md §1 D7; issue #57 Slice 8's
    # `--auth-*` flags). Extracted into `_add_config_parser()` purely to keep
    # `build_parser()` itself under ruff's `PLR0915` statement-count budget -- no
    # behavioral difference from inlining, same rationale as `_add_export_parser()`.
    _add_config_parser(top_level_subparsers, verbose_parent_parser)

    # `auth` subcommand group (issue #57 Slice 13): OIDC device-flow login. See
    # `_add_auth_parser`'s own docstring for why it, like `config`, is factored into a
    # helper rather than inlined.
    _add_auth_parser(top_level_subparsers, verbose_parent_parser)

    # `restore` (restore a curated instrument's artifact into PS Service): reads the
    # artifact off `curated_repo_path` locally and uploads it (`POST /restorations`),
    # reusing `_resolve_client` exactly like `ingest document`.
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

    # `export` (export an already-ingested instrument's baseline/native graphs plus a
    # generated manifest into local files, issue #71): mirrors `restore`'s block above,
    # in the opposite direction. See `_add_export_parser`'s own docstring for why this
    # one verb group is factored into a helper rather than inlined like every other.
    _add_export_parser(top_level_subparsers, verbose_parent_parser)

    return parser
