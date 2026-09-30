"""PS Service composition-root configuration: `ServiceConfig` and `load_config()`.

`main.py` is the process harness's composition root — it resolves the full
config surface exactly once via `load_config()` and injects the result
explicitly into everything that needs it (`uvicorn.run`, `Logging.configure`),
rather than letting components independently read `os.environ`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from ps_service.curated_source.errors import CuratedSourceConfigurationError
from ps_service.curated_source.source_url import validate_source_url

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_DEFAULT_GRACEFUL_SHUTDOWN_SECONDS = 10
_MIN_PORT = 1
_MAX_PORT = 65535
_DEFAULT_FALKORDB_HOST = "127.0.0.1"
_DEFAULT_FALKORDB_PORT = 6379
_DEFAULT_PASSKEY_SIGNING_POSTGRES_PORT = 5432
_DEFAULT_STATE_POSTGRES_PORT = 5432
_DEFAULT_MAX_REQUEST_BODY_BYTES = 104_857_600  # 100 MiB (CHANGES.md OQ7)
_DEFAULT_QUERY_TIMEOUT_MS = 5000
_DEFAULT_QUERY_ROW_CAP = 1000
# D-DEFAULT-URL (issue #125, AC-BI-001): this repo's own public GitHub remote,
# `raw.githubusercontent.com`-served, at the default branch's `curated-content/`
# tree -- the same layout `ps_cli.catalog_repo` already reads locally
# (`{base}/catalog.json`, `{base}/{instrument_id}/manifest.json` etc.).
_DEFAULT_CURATED_SOURCE_BASE_URL = (
    "https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/curated-content"
)

# The fixed caller identity attached to every query answered while the
# local-test bypass (issue #67, AC-BI-008) is active. Colocated with the
# bypass flag since it is a fact *about* the bypass concept, not a variable.
LOCAL_TEST_PRINCIPAL_ID: Final = "local-test-bypass"


class ServiceConfigurationError(Exception):
    """`PS_SERVICE_*`/`PS_LOGGING_DIR` could not be resolved into a valid `ServiceConfig`."""


@dataclass(frozen=True)
class ServiceConfig:
    """Fully-resolved PS Service process configuration.

    Immutable by design: once `load_config()` resolves the environment into
    a `ServiceConfig`, nothing downstream may mutate it.

    `llm_interface_model`/`llm_interface_embed_model` (from
    `PS_LLMINTERFACE_MODEL`/`PS_LLMINTERFACE_EMBED_MODEL`) are `<provider>/<model>`
    strings passed straight through to `litellm.completion`/`litellm.embedding` —
    see `_parse_model_string` and CONTRIBUTING.md for format/examples.

    `falkordb_host`/`falkordb_port` (from `PS_FALKORDB_HOST`/`PS_FALKORDB_PORT`)
    are the connection knobs `ps_service.ingestion.falkordb_client.connect_from_config`
    (Increment 12) uses to build the real FalkorDB connection — see
    PLAN_REVIEWED.md §6.1/§6.4. No `falkordb_graph` field: `PS_FALKORDB_GRAPH`
    already names a different thing (the query-surface's single company
    graph name); Ingestion computes its own per-regulation graph name from
    `short_name` via `native_graph_name`, not from config.

    `company_merge_similarity_threshold` (from
    `PS_COMPANYMERGE_SIMILARITY_THRESHOLD`) is deliberately optional with no
    default here: this field only records what the environment resolved to,
    if anything. Whether a value is *required* is enforced at
    `ps_service.company_merge.merge.merge_baseline_graph`'s own call site,
    not here — `load_config()` is called unconditionally by every process
    entrypoint (`main.py`) and by tests for components that have nothing to
    do with Company Merge, so this layer must never fail closed on the env
    var simply being absent. See PLAN_REVIEWED.md §8 (issue #16, B1's fix).

    `auth_issuer`/`auth_audience`/`auth_cli_client_id`/`auth_scopes` (from
    `PS_AUTH_ISSUER`/`PS_AUTH_AUDIENCE`/`PS_AUTH_CLI_CLIENT_ID`/`PS_AUTH_SCOPES`,
    issue #58) follow the exact same "record what the environment resolved
    to, absence is not an error here" shape as
    `company_merge_similarity_threshold` -- whether both `auth_issuer` and
    `auth_audience` are *required* (fail-closed when the local-test bypass,
    issue #67, is inactive) is enforced by
    `ps_service.auth.startup.resolve_auth_context`'s own call site inside
    `create_app`, not by `load_config()`.

    `curated_source_base_url`/`curated_source_allow_insecure_http` (from
    `PS_CURATEDSOURCE_URL`/`PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP`, issue #125)
    configure where `GET /catalog` fetches the curated-content catalog
    listing from at runtime (AC-BI-001/002/003). Both are validated through
    the shared `ps_service.curated_source.source_url.validate_source_url`
    guard at load time (AC-BI-008/010) -- the same function the runtime
    `set-catalog-source` MCP tool (Slice 3) validates a new override URL
    through, so both surfaces reject exactly the same inputs.

    `passkey_signing_postgres_host`/`_port`/`_database`/`_user`/`_password`
    (from `PS_PASSKEYSIGNING_POSTGRES_HOST`/`_PORT`/`_DATABASE`/`_USER`/
    `_PASSWORD`, issue #131) configure `ps_service.passkey_signing.store.
    connect_from_config`'s connection to PS Service's own, distinct
    Postgres instance (PLAN.md §1.3) -- follows the exact same
    "record what the environment resolved to, absence is not an error here"
    optional shape as `falkordb_host`/`falkordb_port` and
    `company_merge_similarity_threshold` above; whether these are *required*
    is enforced at the Passkey Signing component's own call sites (its
    migration runner / connection opener), not here. `_password` is the
    first credential value `ServiceConfig` itself carries (every other
    credential in this codebase -- LLM provider keys, FalkorDB access -- is
    resolved independently, per `level2-python-instructions.md:48`); it is
    excluded from this dataclass's `repr()` (`field(repr=False)` below) so
    it is never accidentally logged via `repr(config)`/`%r`/an uncaught
    exception's local-variable dump.

    `state_postgres_host`/`_port`/`_database`/`_user`/`_password` (from
    `PS_STATE_POSTGRES_HOST`/`_PORT`/`_DATABASE`/`_USER`/`_PASSWORD`, issue
    #133) configure `ps_service.persistence.connect_from_config`'s
    connection to PS Service's own, distinct, second Postgres instance
    (PLAN.md §0.3/§1.3/§1.4) -- a deliberately independent copy of the
    `passkey_signing_postgres_*` pattern above (own tables, own
    migration-tracking table, own config block), never sharing fields or
    tables with Passkey Signing even if an operator points both at the same
    physical instance. Follows the same "record what the environment
    resolved to, absence is not an error here" optional-at-load-time shape
    -- but unlike `passkey_signing_postgres_*`, an unset
    `state_postgres_host` is *not* treated as "not applicable" by
    `ps_service.persistence.connect_from_config`'s own callers: every one of them fails closed
    (raises `StatePostgresConnectionError`) when unset, since every
    role-gated action must deny by default rather than silently fall open
    (PLAN.md §0.11).

    `authz_bootstrap_owner_subject`/`_issuer` (from
    `PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT`/`PS_AUTHZ_BOOTSTRAP_OWNER_ISSUER`,
    issue #144) are the operator-configured expected first-owner identity
    (matching `Principal.sub`/`Principal.iss`): only a principal whose
    `(sub, iss)` matches these two values is ever granted `SYSTEM_OWNER` by
    `ps_service.authz.store.PsycopgAccessRoleStore.bootstrap_first_owner`.
    Follows the exact same "record what the environment resolved to, absence
    is not an error here" optional-at-load-time shape as `auth_issuer`/
    `auth_audience` above -- whether both are *required* (fail-closed when
    the local-test bypass is inactive) is enforced by
    `ps_service.authz.startup.require_bootstrap_owner_configured`'s own call
    site inside `create_app`, not by `load_config()`.

    `authentik_api_token`/`authentik_base_url` (from `PS_AUTHENTIK_API_TOKEN`/
    `PS_AUTHENTIK_BASE_URL`, issue #140) are PS Service's own service
    credential for calling Authentik's invitation-stage API on behalf of the
    `invite_user` MCP tool -- never a caller-supplied token. Follows the same
    "record what the environment resolved to, absence is not an error here"
    optional-at-load-time shape as every other credential field above --
    but unlike those, whether both are *required* is enforced
    **unconditionally** (no local-test-bypass exemption) by
    `ps_service.invitations.startup.require_authentik_credential_configured`'s
    own call site inside `create_app`, immediately after
    `require_bootstrap_owner_configured` (CHANGES.md #140 Row 2: AC-BI-003's
    wording carries no bypass carve-out, unlike AC-BI-002's). `authentik_api_token`
    is excluded from this dataclass's `repr()` (`field(repr=False)`,
    AC-BI-004) for the same reason `passkey_signing_postgres_password` is.
    """

    host: str
    port: int
    graceful_shutdown_seconds: int
    logging_dir: Path | None
    llm_interface_model: str | None = None
    llm_interface_embed_model: str | None = None
    falkordb_host: str = _DEFAULT_FALKORDB_HOST
    falkordb_port: int = _DEFAULT_FALKORDB_PORT
    company_merge_similarity_threshold: float | None = None
    is_local_test_bypass_active: bool = False
    max_request_body_bytes: int = _DEFAULT_MAX_REQUEST_BODY_BYTES
    query_timeout_ms: int = _DEFAULT_QUERY_TIMEOUT_MS
    query_row_cap: int = _DEFAULT_QUERY_ROW_CAP
    auth_issuer: str | None = None
    auth_audience: str | None = None
    auth_cli_client_id: str | None = None
    auth_scopes: tuple[str, ...] = ()
    curated_source_base_url: str = _DEFAULT_CURATED_SOURCE_BASE_URL
    curated_source_allow_insecure_http: bool = False
    passkey_signing_postgres_host: str | None = None
    passkey_signing_postgres_port: int = _DEFAULT_PASSKEY_SIGNING_POSTGRES_PORT
    passkey_signing_postgres_database: str | None = None
    passkey_signing_postgres_user: str | None = None
    passkey_signing_postgres_password: str | None = field(default=None, repr=False)
    state_postgres_host: str | None = None
    state_postgres_port: int = _DEFAULT_STATE_POSTGRES_PORT
    state_postgres_database: str | None = None
    state_postgres_user: str | None = None
    state_postgres_password: str | None = field(default=None, repr=False)
    authz_bootstrap_owner_subject: str | None = None
    authz_bootstrap_owner_issuer: str | None = None
    authentik_api_token: str | None = field(default=None, repr=False)
    authentik_base_url: str | None = field(default=None, repr=False)


# The `ServiceConfig` fields the ingestion pipeline (Domain Mapper, Company
# Merge) cannot run without, but that `load_config()` itself leaves optional
# (see `ServiceConfig`'s docstring / B1's fix). Named once here so both the
# request-time guard (`ingestion_orchestration._require_ingestion_config`)
# and the Process Harness's `/ready` startup check (issue #16 follow-up) stay
# in lockstep on exactly which three fields that is.
INGESTION_REQUIRED_CONFIG_FIELDS = (
    "llm_interface_model",
    "llm_interface_embed_model",
    "company_merge_similarity_threshold",
)


def missing_ingestion_config_fields(config: ServiceConfig) -> list[str]:
    """Return the `INGESTION_REQUIRED_CONFIG_FIELDS` names that are unset (`None`) on `config`."""
    return [name for name in INGESTION_REQUIRED_CONFIG_FIELDS if getattr(config, name) is None]


def _parse_port(raw: str) -> int:
    """Parse and range-check `PS_SERVICE_PORT`, failing closed on any invalid value."""
    try:
        port = int(raw)
    except ValueError as exc:
        message = f"PS_SERVICE_PORT must be an integer, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if not (_MIN_PORT <= port <= _MAX_PORT):
        message = f"PS_SERVICE_PORT must be between {_MIN_PORT} and {_MAX_PORT}, got {port}"
        raise ServiceConfigurationError(message)
    return port


def _parse_host(raw: str) -> str:
    """Validate `PS_SERVICE_HOST`, rejecting an explicitly-set empty/whitespace-only value.

    Never widens to a fallback value (e.g. `0.0.0.0`) on a bad value —
    "fails closed" means raising, not silently substituting a wider bind.
    """
    if not raw.strip():
        message = "PS_SERVICE_HOST must not be empty or whitespace-only"
        raise ServiceConfigurationError(message)
    return raw


def _parse_graceful_shutdown_seconds(raw: str) -> int:
    """Parse and validate `PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS`.

    Not covered by a dedicated AC, but validated for consistency with the
    host/port validation surface: a malformed value fails closed with the
    same typed error rather than raising a raw `ValueError`.
    """
    try:
        graceful_shutdown_seconds = int(raw)
    except ValueError as exc:
        message = f"PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS must be an integer, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if graceful_shutdown_seconds < 0:
        message = (
            "PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS must not be negative, "
            f"got {graceful_shutdown_seconds}"
        )
        raise ServiceConfigurationError(message)
    return graceful_shutdown_seconds


def _parse_falkordb_port(raw: str) -> int:
    """Parse and range-check `PS_FALKORDB_PORT`, failing closed on any invalid value.

    Mirrors `_parse_port`'s exact validation shape (same integer parse,
    same `_MIN_PORT`/`_MAX_PORT` range check) — a separate function, not a
    reused one, because the error message must name `PS_FALKORDB_PORT`, not
    `PS_SERVICE_PORT`.
    """
    try:
        port = int(raw)
    except ValueError as exc:
        message = f"PS_FALKORDB_PORT must be an integer, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if not (_MIN_PORT <= port <= _MAX_PORT):
        message = f"PS_FALKORDB_PORT must be between {_MIN_PORT} and {_MAX_PORT}, got {port}"
        raise ServiceConfigurationError(message)
    return port


def _parse_falkordb_host(raw: str) -> str:
    """Validate `PS_FALKORDB_HOST`, rejecting an explicitly-set empty/whitespace-only value.

    Mirrors `_parse_host`'s exact validation shape (never widens to a
    fallback value on a bad value — "fails closed" means raising) — a
    separate function, not a reused one, because the error message must
    name `PS_FALKORDB_HOST`, not `PS_SERVICE_HOST`.
    """
    if not raw.strip():
        message = "PS_FALKORDB_HOST must not be empty or whitespace-only"
        raise ServiceConfigurationError(message)
    return raw


def _parse_similarity_threshold(raw: str) -> float:
    """Parse `PS_COMPANYMERGE_SIMILARITY_THRESHOLD`, failing closed on any invalid value.

    Mirrors `_parse_falkordb_port`'s exact validation shape (parse, then
    range-check, raising `ServiceConfigurationError` naming the env var and
    the invalid value on either failure). Only ever called when the env var
    is actually set — absence is not an error at this layer (B1's fix: the
    "fail closed on missing" requirement is enforced by
    `merge_baseline_graph`'s own call site, not here — `load_config()` has
    no way to know whether Company Merge is even going to be used in a given
    process invocation, and every other unrelated caller of `load_config()`
    must keep working with this env var unset).
    """
    try:
        threshold = float(raw)
    except ValueError as exc:
        message = f"PS_COMPANYMERGE_SIMILARITY_THRESHOLD must be a float, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if not (0.0 < threshold <= 1.0):
        message = (
            "PS_COMPANYMERGE_SIMILARITY_THRESHOLD must be greater than 0.0 and at "
            f"most 1.0, got {threshold}"
        )
        raise ServiceConfigurationError(message)
    return threshold


def _parse_max_request_body_bytes(raw: str) -> int:
    """Parse and range-check `PS_SERVICE_MAX_REQUEST_BODY_BYTES`, failing closed on any bad value.

    CHANGES.md OQ7: enforced by `_MaxBodySizeMiddleware` (`main.py`) against
    the request's `Content-Length` header, before Starlette reads any body
    bytes. Mirrors `_parse_port`'s exact validation shape (parse, then
    range-check), but the message names this env var.
    """
    try:
        max_bytes = int(raw)
    except ValueError as exc:
        message = f"PS_SERVICE_MAX_REQUEST_BODY_BYTES must be an integer, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if max_bytes <= 0:
        message = f"PS_SERVICE_MAX_REQUEST_BODY_BYTES must be positive, got {max_bytes}"
        raise ServiceConfigurationError(message)
    return max_bytes


def _parse_query_timeout_ms(raw: str) -> int:
    """Parse and range-check `PS_QUERY_TIMEOUT_MS`, failing closed on any bad value.

    Mirrors `_parse_max_request_body_bytes`'s exact validation shape (parse,
    then positivity check), but the message names this env var. Used as the
    FalkorDB-native `timeout=` argument (milliseconds) `execute_cypher_query`
    passes to `graph.query`.
    """
    try:
        query_timeout_ms = int(raw)
    except ValueError as exc:
        message = f"PS_QUERY_TIMEOUT_MS must be an integer, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if query_timeout_ms <= 0:
        message = f"PS_QUERY_TIMEOUT_MS must be positive, got {query_timeout_ms}"
        raise ServiceConfigurationError(message)
    return query_timeout_ms


def _parse_query_row_cap(raw: str) -> int:
    """Parse and range-check `PS_QUERY_ROW_CAP`, failing closed on any bad value.

    Mirrors `_parse_max_request_body_bytes`'s exact validation shape (parse,
    then positivity check), but the message names this env var. Used as the
    maximum number of rows `execute_cypher_query` returns from a single
    Cypher query, truncating any larger result set in Python after
    `graph.query()` returns (never a FalkorDB-side `RESULTSET_SIZE`).
    """
    try:
        query_row_cap = int(raw)
    except ValueError as exc:
        message = f"PS_QUERY_ROW_CAP must be an integer, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if query_row_cap <= 0:
        message = f"PS_QUERY_ROW_CAP must be positive, got {query_row_cap}"
        raise ServiceConfigurationError(message)
    return query_row_cap


def _parse_local_test_bypass(raw: str | None) -> bool:
    """Parse `PS_SERVICE_LOCAL_TEST_BYPASS`, failing closed on any unrecognized value.

    Unset or empty/whitespace-only (AC-BI-001) resolves to inactive (`False`)
    rather than raising — an operator who never set the var, or set it to
    `""`, gets silence, not a crash. Only `"true"`/`"false"` (case-insensitive)
    are recognized; anything else fails config loading closed (AC-BI-006)
    rather than silently defaulting, mirroring `_parse_port`'s message shape.
    """
    if raw is None or not raw.strip():
        return False
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    message = f"PS_SERVICE_LOCAL_TEST_BYPASS must be 'true' or 'false', got {raw!r}"
    raise ServiceConfigurationError(message)


def _parse_curated_source_allow_insecure_http(raw: str | None) -> bool:
    """Parse `PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP`, failing closed on any unrecognized value.

    Mirrors `_parse_local_test_bypass`'s exact shape: unset or empty/
    whitespace-only resolves to `False` (TLS required by default, AC-BI-010)
    rather than raising. Only `"true"`/`"false"` (case-insensitive) are
    recognized; anything else fails config loading closed (AC-BI-008/010).
    """
    if raw is None or not raw.strip():
        return False
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    message = f"PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP must be 'true' or 'false', got {raw!r}"
    raise ServiceConfigurationError(message)


def _parse_curated_source_base_url(raw: str, *, allow_insecure_http: bool) -> str:
    """Validate `PS_CURATEDSOURCE_URL` via the shared http(s)/TLS guard (D-VALIDATION).

    Delegates to `curated_source.source_url.validate_source_url` -- the same
    function the runtime `set-catalog-source` MCP tool (Slice 3) validates a
    new override URL through -- and translates its
    `CuratedSourceConfigurationError` into `ServiceConfigurationError` so
    this layer's failure type stays uniform with every other `load_config()`
    validation failure (AC-BI-001/008/010).
    """
    try:
        return validate_source_url(raw, allow_insecure_http=allow_insecure_http)
    except CuratedSourceConfigurationError as exc:
        raise ServiceConfigurationError(str(exc)) from exc


def _parse_model_string(raw: str, *, env_var_name: str) -> str:
    """Validate a `PS_LLMINTERFACE_MODEL`/`PS_LLMINTERFACE_EMBED_MODEL` value.

    Mirrors `_parse_host`'s "never widen to a fallback" style: rejects an
    explicitly-set empty/whitespace-only value rather than silently treating
    it as unset.

    Only checks non-empty — this is *not* where format is enforced. The
    expected shape is a LiteLLM-recognized `<provider>/<model-or-deployment-name>`
    string, e.g. `azure/gpt-5.4-mini` or `ollama/phi3:mini`. Provider
    credentials (API keys, base URLs) are separate env vars that LiteLLM
    resolves itself — never part of `ServiceConfig`. See CONTRIBUTING.md
    ("Configure the LLM Interface") for the full worked examples.
    """
    if not raw.strip():
        message = f"{env_var_name} must not be empty or whitespace-only"
        raise ServiceConfigurationError(message)
    return raw


def _parse_auth_string(raw: str, *, env_var_name: str) -> str:
    """Validate a `PS_AUTH_ISSUER`/`PS_AUTH_AUDIENCE`/`PS_AUTH_CLI_CLIENT_ID` value.

    Mirrors `_parse_host`'s "never widen to a fallback" style: rejects an
    explicitly-set empty/whitespace-only value rather than silently treating
    it as unset. Only called when the env var is actually set -- absence is
    not an error at this layer; whether `auth_issuer`/`auth_audience` are
    *required* is enforced by `ps_service.auth.startup.resolve_auth_context`,
    not here (see `ServiceConfig`'s docstring).
    """
    if not raw.strip():
        message = f"{env_var_name} must not be empty or whitespace-only"
        raise ServiceConfigurationError(message)
    return raw


def _parse_passkey_signing_postgres_port(raw: str) -> int:
    """Parse and range-check `PS_PASSKEYSIGNING_POSTGRES_PORT`, failing closed on any bad value.

    Mirrors `_parse_falkordb_port`'s exact validation shape (same integer
    parse, same `_MIN_PORT`/`_MAX_PORT` range check) -- a separate function,
    not a reused one, because the error message must name
    `PS_PASSKEYSIGNING_POSTGRES_PORT`, not `PS_FALKORDB_PORT`.
    """
    try:
        port = int(raw)
    except ValueError as exc:
        message = f"PS_PASSKEYSIGNING_POSTGRES_PORT must be an integer, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if not (_MIN_PORT <= port <= _MAX_PORT):
        message = (
            f"PS_PASSKEYSIGNING_POSTGRES_PORT must be between {_MIN_PORT} and {_MAX_PORT}, "
            f"got {port}"
        )
        raise ServiceConfigurationError(message)
    return port


def _parse_passkey_signing_postgres_string(raw: str, *, env_var_name: str) -> str:
    """Validate a `PS_PASSKEYSIGNING_POSTGRES_HOST`/`_DATABASE`/`_USER`/`_PASSWORD` value.

    Mirrors `_parse_auth_string`'s exact shape (reused across
    `auth_issuer`/`auth_audience`/`auth_cli_client_id`, the established
    precedent for one validator shared by several string fields with
    identical rules): rejects an explicitly-set empty/whitespace-only value
    rather than silently treating it as unset. Never includes the raw value
    in its error message -- safe to reuse for `_password` too, since a
    validation failure here must not leak a partial credential into a raised
    exception's message.
    """
    if not raw.strip():
        message = f"{env_var_name} must not be empty or whitespace-only"
        raise ServiceConfigurationError(message)
    return raw


def _parse_state_postgres_port(raw: str) -> int:
    """Parse and range-check `PS_STATE_POSTGRES_PORT`, failing closed on any bad value.

    Mirrors `_parse_passkey_signing_postgres_port`'s exact validation shape
    -- a separate function, not a reused one, because the error message must
    name `PS_STATE_POSTGRES_PORT`, not `PS_PASSKEYSIGNING_POSTGRES_PORT`.
    """
    try:
        port = int(raw)
    except ValueError as exc:
        message = f"PS_STATE_POSTGRES_PORT must be an integer, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if not (_MIN_PORT <= port <= _MAX_PORT):
        message = f"PS_STATE_POSTGRES_PORT must be between {_MIN_PORT} and {_MAX_PORT}, got {port}"
        raise ServiceConfigurationError(message)
    return port


def _parse_state_postgres_string(raw: str, *, env_var_name: str) -> str:
    """Validate a `PS_STATE_POSTGRES_HOST`/`_DATABASE`/`_USER`/`_PASSWORD` value.

    Mirrors `_parse_passkey_signing_postgres_string`'s exact shape: rejects
    an explicitly-set empty/whitespace-only value rather than silently
    treating it as unset. Never includes the raw value in its error message
    -- safe to reuse for `_password` too.
    """
    if not raw.strip():
        message = f"{env_var_name} must not be empty or whitespace-only"
        raise ServiceConfigurationError(message)
    return raw


def _parse_authz_bootstrap_owner_string(raw: str, *, env_var_name: str) -> str:
    """Validate a `PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT`/`_ISSUER` value.

    Mirrors `_parse_state_postgres_string`'s exact shape: rejects an
    explicitly-set empty/whitespace-only value rather than silently treating
    it as unset -- never widens to a fallback. Only called when the env var
    is actually set -- absence is not an error at this layer; whether both
    are *required* is enforced by
    `ps_service.authz.startup.require_bootstrap_owner_configured`, not here
    (see `ServiceConfig`'s docstring).
    """
    if not raw.strip():
        message = f"{env_var_name} must not be empty or whitespace-only"
        raise ServiceConfigurationError(message)
    return raw


def _parse_authentik_api_token(raw: str) -> str:
    """Validate `PS_AUTHENTIK_API_TOKEN`.

    Mirrors `_parse_passkey_signing_postgres_string`'s exact shape: rejects
    an explicitly-set empty/whitespace-only value rather than silently
    treating it as unset -- never widens to a fallback. Never includes the
    raw value in its error message (AC-BI-004): a validation failure here
    must not leak a partial credential into a raised exception's message.
    Only called when the env var is actually set -- absence is not an error
    at this layer; whether it is *required* is enforced by
    `ps_service.invitations.startup.require_authentik_credential_configured`,
    not here (see `ServiceConfig`'s docstring).
    """
    if not raw.strip():
        message = "PS_AUTHENTIK_API_TOKEN must not be empty or whitespace-only"
        raise ServiceConfigurationError(message)
    return raw


def _parse_authentik_base_url(raw: str) -> str:
    """Validate `PS_AUTHENTIK_BASE_URL`.

    Mirrors `_parse_authentik_api_token`'s exact shape: rejects an
    explicitly-set empty/whitespace-only value rather than silently treating
    it as unset. Only checks non-empty -- URL scheme/shape validation is not
    required by any AC for this field, unlike `curated_source_base_url`'s
    dedicated TLS guard.
    """
    if not raw.strip():
        message = "PS_AUTHENTIK_BASE_URL must not be empty or whitespace-only"
        raise ServiceConfigurationError(message)
    return raw


def _resolve_authentik_api_token() -> str | None:
    """Resolve `PS_AUTHENTIK_API_TOKEN`, validating it if set.

    Factored out of `load_config()` itself (unlike every other optional
    credential field's inline "raw env value, parse if not `None`" shape)
    solely to keep `load_config` under this project's `PLR0915`
    max-statements budget -- calling this from inside the final
    `ServiceConfig(...)` constructor call adds no new statement to
    `load_config`'s own body. Functionally identical to inlining it.
    """
    raw = os.environ.get("PS_AUTHENTIK_API_TOKEN")
    return _parse_authentik_api_token(raw) if raw is not None else None


def _resolve_authentik_base_url() -> str | None:
    """Resolve `PS_AUTHENTIK_BASE_URL`, validating it if set.

    Mirrors `_resolve_authentik_api_token`'s exact shape and rationale.
    """
    raw = os.environ.get("PS_AUTHENTIK_BASE_URL")
    return _parse_authentik_base_url(raw) if raw is not None else None


def _parse_auth_scopes(raw: str) -> tuple[str, ...]:
    """Parse `PS_AUTH_SCOPES` into an informational-only tuple of scope names.

    Split on commas and whitespace, discarding empty entries -- never
    enforced as authorization (issue #58's TASK.md: "audience check only").
    Only called when the env var is actually set; unset resolves to `()` in
    `load_config()` directly, without calling this function.
    """
    return tuple(part for part in raw.replace(",", " ").split() if part)


def load_config() -> ServiceConfig:
    """Resolve `PS_SERVICE_*`/`PS_LOGGING_DIR` into a `ServiceConfig`.

    Reads the environment exactly once. Unset variables fall back to the
    defaults matching issue #12's originally-shipped hardcoded values.
    Raises `ServiceConfigurationError` if any resolved value is invalid,
    before any other part of the configuration is used.
    """
    host = _parse_host(os.environ.get("PS_SERVICE_HOST", _DEFAULT_HOST))
    port = _parse_port(os.environ.get("PS_SERVICE_PORT", str(_DEFAULT_PORT)))
    graceful_shutdown_seconds = _parse_graceful_shutdown_seconds(
        os.environ.get(
            "PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS",
            str(_DEFAULT_GRACEFUL_SHUTDOWN_SECONDS),
        )
    )
    logging_dir_raw = os.environ.get("PS_LOGGING_DIR")
    logging_dir = Path(logging_dir_raw) if logging_dir_raw is not None else None

    llm_interface_model_raw = os.environ.get("PS_LLMINTERFACE_MODEL")
    llm_interface_model = (
        _parse_model_string(llm_interface_model_raw, env_var_name="PS_LLMINTERFACE_MODEL")
        if llm_interface_model_raw is not None
        else None
    )
    llm_interface_embed_model_raw = os.environ.get("PS_LLMINTERFACE_EMBED_MODEL")
    llm_interface_embed_model = (
        _parse_model_string(
            llm_interface_embed_model_raw, env_var_name="PS_LLMINTERFACE_EMBED_MODEL"
        )
        if llm_interface_embed_model_raw is not None
        else None
    )

    falkordb_host = _parse_falkordb_host(os.environ.get("PS_FALKORDB_HOST", _DEFAULT_FALKORDB_HOST))
    falkordb_port = _parse_falkordb_port(
        os.environ.get("PS_FALKORDB_PORT", str(_DEFAULT_FALKORDB_PORT))
    )

    company_merge_similarity_threshold_raw = os.environ.get("PS_COMPANYMERGE_SIMILARITY_THRESHOLD")
    company_merge_similarity_threshold = (
        _parse_similarity_threshold(company_merge_similarity_threshold_raw)
        if company_merge_similarity_threshold_raw is not None
        else None
    )

    is_local_test_bypass_active = _parse_local_test_bypass(
        os.environ.get("PS_SERVICE_LOCAL_TEST_BYPASS")
    )

    max_request_body_bytes = _parse_max_request_body_bytes(
        os.environ.get("PS_SERVICE_MAX_REQUEST_BODY_BYTES", str(_DEFAULT_MAX_REQUEST_BODY_BYTES))
    )

    query_timeout_ms = _parse_query_timeout_ms(
        os.environ.get("PS_QUERY_TIMEOUT_MS", str(_DEFAULT_QUERY_TIMEOUT_MS))
    )
    query_row_cap = _parse_query_row_cap(
        os.environ.get("PS_QUERY_ROW_CAP", str(_DEFAULT_QUERY_ROW_CAP))
    )

    auth_issuer_raw = os.environ.get("PS_AUTH_ISSUER")
    auth_issuer = (
        _parse_auth_string(auth_issuer_raw, env_var_name="PS_AUTH_ISSUER")
        if auth_issuer_raw is not None
        else None
    )
    auth_audience_raw = os.environ.get("PS_AUTH_AUDIENCE")
    auth_audience = (
        _parse_auth_string(auth_audience_raw, env_var_name="PS_AUTH_AUDIENCE")
        if auth_audience_raw is not None
        else None
    )
    auth_cli_client_id_raw = os.environ.get("PS_AUTH_CLI_CLIENT_ID")
    auth_cli_client_id = (
        _parse_auth_string(auth_cli_client_id_raw, env_var_name="PS_AUTH_CLI_CLIENT_ID")
        if auth_cli_client_id_raw is not None
        else None
    )
    auth_scopes_raw = os.environ.get("PS_AUTH_SCOPES")
    auth_scopes = _parse_auth_scopes(auth_scopes_raw) if auth_scopes_raw is not None else ()

    curated_source_allow_insecure_http = _parse_curated_source_allow_insecure_http(
        os.environ.get("PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP")
    )
    curated_source_base_url = _parse_curated_source_base_url(
        os.environ.get("PS_CURATEDSOURCE_URL", _DEFAULT_CURATED_SOURCE_BASE_URL),
        allow_insecure_http=curated_source_allow_insecure_http,
    )

    passkey_signing_postgres_host_raw = os.environ.get("PS_PASSKEYSIGNING_POSTGRES_HOST")
    passkey_signing_postgres_host = (
        _parse_passkey_signing_postgres_string(
            passkey_signing_postgres_host_raw, env_var_name="PS_PASSKEYSIGNING_POSTGRES_HOST"
        )
        if passkey_signing_postgres_host_raw is not None
        else None
    )
    passkey_signing_postgres_port = _parse_passkey_signing_postgres_port(
        os.environ.get(
            "PS_PASSKEYSIGNING_POSTGRES_PORT", str(_DEFAULT_PASSKEY_SIGNING_POSTGRES_PORT)
        )
    )
    passkey_signing_postgres_database_raw = os.environ.get("PS_PASSKEYSIGNING_POSTGRES_DATABASE")
    passkey_signing_postgres_database = (
        _parse_passkey_signing_postgres_string(
            passkey_signing_postgres_database_raw,
            env_var_name="PS_PASSKEYSIGNING_POSTGRES_DATABASE",
        )
        if passkey_signing_postgres_database_raw is not None
        else None
    )
    passkey_signing_postgres_user_raw = os.environ.get("PS_PASSKEYSIGNING_POSTGRES_USER")
    passkey_signing_postgres_user = (
        _parse_passkey_signing_postgres_string(
            passkey_signing_postgres_user_raw, env_var_name="PS_PASSKEYSIGNING_POSTGRES_USER"
        )
        if passkey_signing_postgres_user_raw is not None
        else None
    )
    passkey_signing_postgres_password_raw = os.environ.get("PS_PASSKEYSIGNING_POSTGRES_PASSWORD")
    passkey_signing_postgres_password = (
        _parse_passkey_signing_postgres_string(
            passkey_signing_postgres_password_raw,
            env_var_name="PS_PASSKEYSIGNING_POSTGRES_PASSWORD",
        )
        if passkey_signing_postgres_password_raw is not None
        else None
    )

    state_postgres_host_raw = os.environ.get("PS_STATE_POSTGRES_HOST")
    state_postgres_host = (
        _parse_state_postgres_string(state_postgres_host_raw, env_var_name="PS_STATE_POSTGRES_HOST")
        if state_postgres_host_raw is not None
        else None
    )
    state_postgres_port = _parse_state_postgres_port(
        os.environ.get("PS_STATE_POSTGRES_PORT", str(_DEFAULT_STATE_POSTGRES_PORT))
    )
    state_postgres_database_raw = os.environ.get("PS_STATE_POSTGRES_DATABASE")
    state_postgres_database = (
        _parse_state_postgres_string(
            state_postgres_database_raw, env_var_name="PS_STATE_POSTGRES_DATABASE"
        )
        if state_postgres_database_raw is not None
        else None
    )
    state_postgres_user_raw = os.environ.get("PS_STATE_POSTGRES_USER")
    state_postgres_user = (
        _parse_state_postgres_string(state_postgres_user_raw, env_var_name="PS_STATE_POSTGRES_USER")
        if state_postgres_user_raw is not None
        else None
    )
    state_postgres_password_raw = os.environ.get("PS_STATE_POSTGRES_PASSWORD")
    state_postgres_password = (
        _parse_state_postgres_string(
            state_postgres_password_raw, env_var_name="PS_STATE_POSTGRES_PASSWORD"
        )
        if state_postgres_password_raw is not None
        else None
    )

    authz_bootstrap_owner_subject_raw = os.environ.get("PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT")
    authz_bootstrap_owner_subject = (
        _parse_authz_bootstrap_owner_string(
            authz_bootstrap_owner_subject_raw, env_var_name="PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT"
        )
        if authz_bootstrap_owner_subject_raw is not None
        else None
    )
    authz_bootstrap_owner_issuer_raw = os.environ.get("PS_AUTHZ_BOOTSTRAP_OWNER_ISSUER")
    authz_bootstrap_owner_issuer = (
        _parse_authz_bootstrap_owner_string(
            authz_bootstrap_owner_issuer_raw, env_var_name="PS_AUTHZ_BOOTSTRAP_OWNER_ISSUER"
        )
        if authz_bootstrap_owner_issuer_raw is not None
        else None
    )

    return ServiceConfig(
        host=host,
        port=port,
        graceful_shutdown_seconds=graceful_shutdown_seconds,
        logging_dir=logging_dir,
        llm_interface_model=llm_interface_model,
        llm_interface_embed_model=llm_interface_embed_model,
        falkordb_host=falkordb_host,
        falkordb_port=falkordb_port,
        company_merge_similarity_threshold=company_merge_similarity_threshold,
        is_local_test_bypass_active=is_local_test_bypass_active,
        max_request_body_bytes=max_request_body_bytes,
        query_timeout_ms=query_timeout_ms,
        query_row_cap=query_row_cap,
        auth_issuer=auth_issuer,
        auth_audience=auth_audience,
        auth_cli_client_id=auth_cli_client_id,
        auth_scopes=auth_scopes,
        curated_source_base_url=curated_source_base_url,
        curated_source_allow_insecure_http=curated_source_allow_insecure_http,
        passkey_signing_postgres_host=passkey_signing_postgres_host,
        passkey_signing_postgres_port=passkey_signing_postgres_port,
        passkey_signing_postgres_database=passkey_signing_postgres_database,
        passkey_signing_postgres_user=passkey_signing_postgres_user,
        passkey_signing_postgres_password=passkey_signing_postgres_password,
        state_postgres_host=state_postgres_host,
        state_postgres_port=state_postgres_port,
        state_postgres_database=state_postgres_database,
        state_postgres_user=state_postgres_user,
        state_postgres_password=state_postgres_password,
        authz_bootstrap_owner_subject=authz_bootstrap_owner_subject,
        authz_bootstrap_owner_issuer=authz_bootstrap_owner_issuer,
        authentik_api_token=_resolve_authentik_api_token(),
        authentik_base_url=_resolve_authentik_base_url(),
    )
