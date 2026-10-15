"""ps-cli's argparse definition: `build_parser()`.

Builds the complete command-line surface (`get`, `ingest`, `config`, `auth`,
`export`) with its shared flags and parse-time validators. This module only
builds the parser; it never calls `parse_args()` itself, which is
`ps_cli.cli.run()`'s job.
"""

from __future__ import annotations

import argparse
import re

from ps_cli.config import is_valid_service_url

# A context name is always a safe bare TOML key by construction (alnum start,
# alnum/`_`/`-` body, 1-64 chars) -- no quoting logic needed by `toml_writer`.
# This charset is the CLI's own convention, not taken from an external specification.
_CONTEXT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

# A curated instrument id (e.g. "CRA-1.0", `curated-content/catalog.json`'s real entries)
# is not constrained by any existing server-side pattern -- `RestorationManifestPayload
# .instrument_id`/`CatalogInstrumentEntry.instrument_id` (ps_service/api/models.py) are both
# bare `Field(min_length=1)`, no `pattern=`. This charset is instead this module's own
# documented assumption -- alnum start, alnum/`_`/`-`/`.` body, no `/`, matching every real
# `catalog.json` entry (`CRA-1.0`, `GDPR-1.0`, `ENGPRAC-1.0`) while rejecting a path-traversal
# payload at parse time (L1 "Fail Fast at Boundaries"). `export instrument`'s
# `instrument_id` positional (`_add_export_parser`) uses it, and the strict charset is the
# right validation for an id that will be sent on to PS Service.
_INSTRUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _context_name_type(value: str) -> str:
    """Validate a context name given to `set-context`/`use-context` (an argparse `type=` callback).

    Format validation belongs at parse time, so a bad name exits with usage
    status 2 before any handler runs. The accepted charset is alphanumeric start,
    alphanumeric/`_`/`-` body, 1-64 characters in total; such a name is always a
    safe bare TOML key.

    Args:
        value: The raw command-line text for the context name.

    Returns:
        `value` unchanged when it matches the accepted charset.

    Raises:
        argparse.ArgumentTypeError: `value` does not match the charset; argparse
            reports it and exits with status 2.
    """
    if not _CONTEXT_NAME_PATTERN.fullmatch(value):
        msg = (
            f"'{value}' is not a valid context name (expected: 1-64 characters, "
            "starting with a letter or digit, followed by letters, digits, '_', or '-')"
        )
        raise argparse.ArgumentTypeError(msg)
    return value


def _service_url_type(value: str) -> str:
    """Validate the `--url` value of `config set-context` (an argparse `type=` callback).

    Delegates to `ps_cli.config.is_valid_service_url()` so the parse-time check and
    the configuration loader share one definition of a usable service URL.

    Args:
        value: The raw command-line text for the PS Service URL.

    Returns:
        `value` unchanged when it is a valid http(s) URL.

    Raises:
        argparse.ArgumentTypeError: `value` is not a valid http(s) URL; argparse
            reports it and exits with status 2.
    """
    if not is_valid_service_url(value):
        msg = f"'{value}' is not a valid http(s) URL"
        raise argparse.ArgumentTypeError(msg)
    return value


def _auth_scopes_type(value: str) -> tuple[str, ...]:
    """Split the `--auth-scopes` value into a tuple of scopes (an argparse `type=` callback).

    A single comma-separated flag was chosen over a repeated flag because it reads
    more naturally next to `--url` on one command line, while `targets.toml` stores
    the scopes as an array either way.

    Args:
        value: Comma-separated scope names, e.g. `"openid,profile"`.

    Returns:
        The scopes in order, each stripped of surrounding whitespace. Empty elements
        are kept (`"a,,b"` yields three items).
    """
    return tuple(item.strip() for item in value.split(","))


def _instrument_id_type(value: str) -> str:
    """Validate the `export instrument` `instrument_id` positional (an argparse `type=` callback).

    Accepts an alphanumeric start followed by letters, digits, `_`, `-` or `.`
    (1-128 characters), which excludes `/`; a `..` substring is rejected as well,
    as defence in depth against path traversal. The strict charset is the right
    validation for an id that is sent on to PS Service.

    Args:
        value: The raw command-line text for the instrument id, e.g. `"CRA-1.0"`.

    Returns:
        `value` unchanged when it is a valid instrument id.

    Raises:
        argparse.ArgumentTypeError: `value` is outside the accepted charset or
            contains `..`; argparse reports it and exits with status 2.
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
    """Validate the `document_path` positional of `ingest document` (an argparse `type=` callback).

    Format validation belongs at parse time, not in the handler. The value is a
    plain local filesystem path (relative to the current directory, or absolute),
    resolved the ordinary way and not against any PS Service or ps-cli root.

    Args:
        value: The raw command-line text for the document path.

    Returns:
        `value` unchanged when it is a non-empty path ending in `.json`.

    Raises:
        argparse.ArgumentTypeError: `value` is empty or does not end in `.json`;
            argparse reports it and exits with status 2.
    """
    if not value or not value.endswith(".json"):
        msg = f"'{value}' must be a non-empty path ending in '.json'"
        raise argparse.ArgumentTypeError(msg)
    return value


def _add_export_parser(
    top_level_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]  # argparse: no public alias for add_subparsers()'s return type
    verbose_parent_parser: argparse.ArgumentParser,
) -> None:
    """Add the `export` verb group and its `instrument` leaf to `top_level_subparsers`.

    Wires `export instrument <instrument_id> [destination]` to the
    `export_instrument` command. `destination` is an optional positional
    (`nargs="?"`, default `None`) rather than a `type=` callback: writability is a
    runtime filesystem check, not a format check, so the handler performs it.
    Split out of `build_parser()` only to keep that function within the
    statement-count limit.

    Args:
        top_level_subparsers: The top-level subparsers action to register the
            group on.
        verbose_parent_parser: Parent parser carrying the shared `-v`/`--context`
            flags, inherited by every parser added here.

    Returns:
        None. The parsers are registered on `top_level_subparsers` in place.
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
    """Add the `config` subcommand group to `top_level_subparsers`.

    The group manages named PS Service targets (contexts) in `targets.toml` through
    `set-context`, `use-context` and `get-contexts`. It is the one exception to the
    verb-then-resource shape every other group follows: like kubectl's own
    `kubectl config`, its leaves are verb-noun compounds rather than nested
    `config <verb> <resource>`. Split out of `build_parser()` only to keep that
    function within the statement-count limit.

    Args:
        top_level_subparsers: The top-level subparsers action to register the
            group on.
        verbose_parent_parser: Parent parser carrying the shared `-v`/`--context`
            flags, inherited by every parser added here.

    Returns:
        None. The parsers are registered on `top_level_subparsers` in place.
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
    """Add the four `--auth-*` override flags to `set_context_parser`.

    The flags are per-context OIDC parameter overrides layered onto device-flow
    discovery (`ps_cli.oidc_discovery.resolve_auth_parameters`). All four default to
    `None`; omitting a flag leaves that field untouched by
    `handle_config_set_context`'s per-field merge rather than clearing it. Split out
    of `build_parser()` only to keep that function within the statement-count limit.

    Args:
        set_context_parser: The `config set-context` parser to add the flags to.

    Returns:
        None. The flags are registered on `set_context_parser` in place.
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
    """Add the `auth` command group (`login`, `status`, `logout`) to `top_level_subparsers`.

    The group follows the shape of `gh auth login/status/logout`: three plain-verb
    leaves. Each leaf takes only the shared `-v`/`--context` flags, because the
    context to authenticate is resolved exactly like it is for every other command.
    Split out of `build_parser()` only to keep that function within the
    statement-count limit.

    Args:
        top_level_subparsers: The top-level subparsers action to register the
            group on.
        verbose_parent_parser: Parent parser carrying the shared `-v`/`--context`
            flags, inherited by every parser added here.

    Returns:
        None. The parsers are registered on `top_level_subparsers` in place.
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
        help="Verify the current context's stored credential and show its login status.",
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

    Every top-level group except `config` is a verb (`get`, `ingest`, `export`);
    each verb group holds resource-noun leaves (e.g. `ingest document`,
    `export instrument`), giving `ps-cli <verb> <resource> [name] [flags]`.
    `config` is the one exception group: like `kubectl config`, its leaves are
    verb-noun compounds (`set-context`/`use-context`/`get-contexts`). `auth` adds
    `login`/`status`/`logout`.

    Each leaf sets `command` through `set_defaults()` to the key `run()` looks up in
    its dispatch tables. Every verb group's subparsers are `required=True`, so once a
    group is chosen a successful parse always yields a `command`. The top-level
    group itself is optional: bare `ps-cli` and `ps-cli --version` parse without a
    subcommand and `run()` handles both before dispatch.

    `-v`/`--verbose` and `--context <name>` are shared flags available before or
    after any subcommand. Both use `default=SUPPRESS`: argparse parses each
    subcommand into a fresh namespace and copies every attribute onto the parent
    namespace, so an unset leaf default would otherwise overwrite a value given
    before the subcommand name. As a consequence the attributes are absent, not
    `False`/`None`, when the flags were never given; read them with `getattr`.

    Returns:
        The fully configured `argparse.ArgumentParser` (`prog="ps-cli"`). Each call
        builds a fresh, independent parser; calling it has no side effects.
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
            "(overrides targets.toml's current_context; never persisted)."
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

    # `get` (read-only lookups): service health. Its only leaf, `get health`, reports
    # PS Service's reachability/health/readiness. Catalog listing is reached via the
    # `ps-get-catalog-listing` skill, not this CLI.
    get_parser = top_level_subparsers.add_parser(
        "get",
        parents=[verbose_parent_parser],
        help="Read-only lookups: service health.",
    )
    get_subparsers = get_parser.add_subparsers(dest="get_command", required=True)

    get_health_parser = get_subparsers.add_parser(
        "health",
        parents=[verbose_parent_parser],
        help="Report PS Service's reachability, health (`/health`), and readiness (`/ready`).",
    )
    get_health_parser.set_defaults(command="get_health")

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

    # `config` subcommand group (includes the `--auth-*` flags). Extracted into
    # `_add_config_parser()` purely to keep `build_parser()` under ruff's `PLR0915`
    # statement-count budget -- no behavioral difference from inlining.
    _add_config_parser(top_level_subparsers, verbose_parent_parser)

    # `auth` subcommand group: OIDC device-flow login. Factored into a helper, like
    # `config`, for the same statement-count reason.
    _add_auth_parser(top_level_subparsers, verbose_parent_parser)

    # `export` (export an already-ingested instrument's baseline/native graphs plus a
    # generated manifest into local files). Factored into a helper for the same
    # statement-count reason. Restoring a curated instrument is reached via the
    # `ps-restore-instrument` skill, not this CLI.
    _add_export_parser(top_level_subparsers, verbose_parent_parser)

    return parser
