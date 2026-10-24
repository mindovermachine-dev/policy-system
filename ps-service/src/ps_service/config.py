"""PS Service composition-root configuration: `ServiceConfig` and `load_config()`.

`main.py` is the process harness's composition root — it resolves the full
config surface exactly once via `load_config()` and injects the result
explicitly into everything that needs it (`uvicorn.run`, `Logging.configure`),
rather than letting components independently read `os.environ`.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from ps_service.curated_source.errors import CuratedSourceConfigurationError
from ps_service.curated_source.source_url import validate_source_url

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_DEFAULT_GRACEFUL_SHUTDOWN_SECONDS = 10
_MIN_PORT = 1
_MAX_PORT = 65535
_DEFAULT_FALKORDB_HOST = "127.0.0.1"
_DEFAULT_FALKORDB_PORT = 6379
_DEFAULT_FALKORDB_GRAPH = "policy_system"
_DEFAULT_PASSKEY_SIGNING_POSTGRES_PORT = 5432
_DEFAULT_STATE_POSTGRES_PORT = 5432
_DEFAULT_MAX_REQUEST_BODY_BYTES = 104_857_600  # 100 MiB
_DEFAULT_AUTH_DISCOVERY_RETRY_BUDGET_SECONDS = 300
_DEFAULT_QUERY_TIMEOUT_MS = 5000
_DEFAULT_FALKORDB_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_FALKORDB_SOCKET_TIMEOUT_SECONDS = 30.0
_DEFAULT_STATE_POSTGRES_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_STATE_POSTGRES_STATEMENT_TIMEOUT_MS = 30000
_DEFAULT_PASSKEY_SIGNING_POSTGRES_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_PASSKEY_SIGNING_POSTGRES_STATEMENT_TIMEOUT_MS = 30000
_DEFAULT_QUERY_ROW_CAP = 1000
_DEFAULT_GRAPH_DRIFT_CHECK_INTERVAL_SECONDS = 3600
_DEFAULT_INGESTION_RUNS_MAX_IN_FLIGHT = 1
# This repo's own public GitHub remote, `raw.githubusercontent.com`-served, at the default
# branch's `curated-content/` tree -- the same layout `ps_cli.catalog_repo` reads locally
# (`{base}/catalog.json`, `{base}/{instrument_id}/manifest.json` etc.).
_DEFAULT_CURATED_SOURCE_BASE_URL = (
    "https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/curated-content"
)

# The fixed caller identity attached to every query answered while the local-test bypass is
# active. Colocated with the bypass flag since it is a fact *about* the bypass concept, not a
# variable.
LOCAL_TEST_PRINCIPAL_ID: Final = "local-test-bypass"


class ServiceConfigurationError(Exception):
    """An environment variable could not be resolved into a valid `ServiceConfig`.

    Raised by `load_config()` when a `PS_*` variable holds a malformed, out-of-range, blank or
    unrecognised value. The message names the variable and the offending value, except for
    credential variables, whose values are never echoed. The process must not start; the
    operator should correct the variable.
    """


@dataclass(frozen=True)
class ServiceConfig:
    """Fully-resolved PS Service process configuration.

    Immutable: once `load_config()` resolves the environment into a `ServiceConfig`, nothing
    downstream may mutate it. Optional fields (`None` or an empty tuple) only record that the
    environment did not set a value; whether a value is required is enforced by the consuming
    component at its own call site, because `load_config()` runs in every process entrypoint
    and in tests of components that need none of them. The credential fields
    (`passkey_signing_postgres_password`, `state_postgres_password`, `authentik_api_token`,
    `authentik_base_url`) are excluded from `repr()` so they are never logged by accident.

    Attributes:
        host: Interface the HTTP server binds to (`PS_SERVICE_HOST`); non-blank.
        port: TCP port the HTTP server binds to (`PS_SERVICE_PORT`); 1 to 65535.
        graceful_shutdown_seconds: Seconds the server waits for in-flight requests on shutdown
            (`PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS`); non-negative.
        logging_dir: Directory for log files (`PS_LOGGING_DIR`); `None` when unset.
        llm_interface_model: `<provider>/<model>` string passed straight to LiteLLM's
            completion call (`PS_LLMINTERFACE_MODEL`), for example `azure/gpt-5.4-mini`;
            `None` when unset. Provider credentials are separate environment variables that
            LiteLLM resolves itself and are never part of this config.
        llm_interface_embed_model: Same shape, for LiteLLM's embedding call
            (`PS_LLMINTERFACE_EMBED_MODEL`); `None` when unset.
        falkordb_host: FalkorDB host (`PS_FALKORDB_HOST`); non-blank, default `127.0.0.1`.
        falkordb_port: FalkorDB port (`PS_FALKORDB_PORT`); 1 to 65535, default 6379.
        falkordb_graph: Name of the single-tenant graph every component reads and writes
            (`PS_FALKORDB_GRAPH`); non-blank, default `policy_system`.
        company_merge_capability_merge_threshold: Minimum embedding similarity for Company Merge to
            treat two entities as the same (`PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD`); greater
            than 0.0 and at most 1.0, `None` when unset. Required only when a merge runs.
        company_merge_capability_review_threshold: Minimum embedding similarity for a Capability
            near miss to be raised for review (`PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD`);
            at least 0.0 and at most 1.0, and not above the merge threshold when both are set.
            `0.0` hides nothing. `None` when unset. Required only when a merge runs.
        company_merge_role_review_threshold: Minimum embedding similarity for a Role near miss
            to be raised for review (`PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD`); at least 0.0 and
            at most 1.0. Review-only: a Role is never merged automatically, so there is no Role
            merge threshold. `0.0` hides nothing. `None` when unset. Required only when a merge
            runs.
        is_local_test_bypass_active: Whether authentication is bypassed and every request
            runs as `LOCAL_TEST_PRINCIPAL_ID` (`PS_SERVICE_LOCAL_TEST_BYPASS`); default `False`.
        max_request_body_bytes: Largest accepted request body, checked against the
            `Content-Length` header before any body is read
            (`PS_SERVICE_MAX_REQUEST_BODY_BYTES`); positive, default 100 MiB.
        query_timeout_ms: Server-side time limit for one Cypher query in milliseconds
            (`PS_QUERY_TIMEOUT_MS`); positive, default 5000.
        query_row_cap: Maximum rows one Cypher query returns (`PS_QUERY_ROW_CAP`); positive,
            default 1000.
        falkordb_connect_timeout_seconds: Longest wait to establish a FalkorDB connection, in
            seconds (`PS_FALKORDB_CONNECT_TIMEOUT_SECONDS`); positive and finite, default 5.
        falkordb_socket_timeout_seconds: Longest wait for a FalkorDB reply on an open connection,
            in seconds (`PS_FALKORDB_SOCKET_TIMEOUT_SECONDS`); positive, finite and greater than
            `query_timeout_ms` converted to seconds, default 30.
        state_postgres_connect_timeout_seconds: Longest wait to establish a state Postgres
            connection, in seconds (`PS_STATE_POSTGRES_CONNECT_TIMEOUT_SECONDS`); positive and
            finite, default 5.
        state_postgres_statement_timeout_ms: Longest run time of one state Postgres statement on a
            request-path connection, in milliseconds (`PS_STATE_POSTGRES_STATEMENT_TIMEOUT_MS`);
            positive, default 30000. Startup migrations do not apply it.
        passkey_signing_postgres_connect_timeout_seconds: Longest wait to establish a passkey
            signing Postgres connection, in seconds
            (`PS_PASSKEYSIGNING_POSTGRES_CONNECT_TIMEOUT_SECONDS`); positive and finite, default 5.
        passkey_signing_postgres_statement_timeout_ms: Longest run time of one passkey signing
            Postgres statement, in milliseconds
            (`PS_PASSKEYSIGNING_POSTGRES_STATEMENT_TIMEOUT_MS`); positive, default 30000.
        graph_drift_check_interval_seconds: Pause between two passes of the graph write
            gateway's drift check (`PS_GRAPHGATEWAY_DRIFT_CHECK_INTERVAL_SECONDS`); zero turns
            the periodic check off, default 3600.
        ingestion_runs_max_in_flight: Most ingestion runs executing concurrently in this
            process (`PS_INGESTIONRUNS_MAX_IN_FLIGHT`); positive, default 1.
        auth_issuer: Expected token issuer (`PS_AUTH_ISSUER`); `None` when unset. With
            `auth_audience`, required unless the local-test bypass is active.
        auth_audience: Expected token audience (`PS_AUTH_AUDIENCE`); `None` when unset.
        auth_discovery_retry_budget_seconds: Total time startup keeps retrying a failed OIDC
            discovery fetch before failing closed (`PS_AUTH_DISCOVERY_RETRY_BUDGET_SECONDS`);
            zero means a single attempt, default 300.
        auth_cli_client_id: OAuth client id advertised to command-line clients
            (`PS_AUTH_CLI_CLIENT_ID`); `None` when unset.
        auth_scopes: Informational scope names advertised to clients (`PS_AUTH_SCOPES`, split
            on commas and whitespace); never enforced as authorization. Empty when unset.
        curated_source_base_url: Base URL the curated-content catalog is fetched from
            (`PS_CURATEDSOURCE_URL`); validated by
            `ps_service.curated_source.source_url.validate_source_url`, the same check the
            runtime source override uses. Defaults to this project's public curated-content
            tree.
        curated_source_allow_insecure_http: Whether a plain `http` source URL is accepted
            (`PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP`); default `False` (TLS required).
        passkey_signing_postgres_host: Host of Passkey Signing's own Postgres instance
            (`PS_PASSKEYSIGNING_POSTGRES_HOST`); `None` when unset, in which case Passkey
            Signing treats its store as not applicable.
        passkey_signing_postgres_port: Its port (`PS_PASSKEYSIGNING_POSTGRES_PORT`); 1 to
            65535, default 5432.
        passkey_signing_postgres_database: Its database name; `None` when unset.
        passkey_signing_postgres_user: Its user name; `None` when unset.
        passkey_signing_postgres_password: Its password; `None` when unset.
        passkey_signing_allowed_origins: The origins (`scheme://host[:port]`) the companion-browser
            approval page may be served from (`PS_PASSKEYSIGNING_ALLOWED_ORIGINS`, comma
            separated). The first entry names the WebAuthn relying party and its origin; a POST
            on an approval route whose `Host` matches none is rejected. Empty when unset, in
            which case the relying party is derived from each request's own `Host`.
        public_url: The service's public origin (`PS_PUBLIC_URL`), `https://host[:port]` with no
            path, query, fragment or user info; `http` is accepted only while the local-test
            bypass is active. It is the only source of the origin published in the
            protected-resource metadata and in the `WWW-Authenticate` challenge. `None` when
            unset, which is accepted only while the local-test bypass is active, where the
            origin is derived from each request instead.
        state_postgres_host: Host of the PS state Postgres instance (`PS_STATE_POSTGRES_HOST`),
            separate from Passkey Signing's, with its own tables and migration tracking;
            `None` when unset. Unlike Passkey Signing, an unset host is not "not applicable":
            every state-store caller fails closed with `StatePostgresConnectionError`, so every
            role-gated action denies by default.
        state_postgres_port: Its port (`PS_STATE_POSTGRES_PORT`); 1 to 65535, default 5432.
        state_postgres_database: Its database name; `None` when unset.
        state_postgres_user: Its user name; `None` when unset.
        state_postgres_password: Its password; `None` when unset.
        authz_bootstrap_owner_subject: Token subject (`sub`) of the operator-designated first
            system owner (`PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT`); `None` when unset. Only a
            principal whose `(sub, iss)` matches this and `authz_bootstrap_owner_issuer` is
            granted the system-owner role by bootstrap. Required unless the local-test bypass
            is active.
        authz_bootstrap_owner_issuer: Token issuer (`iss`) of that principal
            (`PS_AUTHZ_BOOTSTRAP_OWNER_ISSUER`); `None` when unset.
        authentik_api_token: PS Service's own credential for calling the identity provider's
            invitation API on behalf of the invite-user tool (`PS_AUTHENTIK_API_TOKEN`);
            never a caller-supplied token; `None` when unset. Required at startup together
            with `authentik_base_url`, with no local-test-bypass exemption.
        authentik_base_url: Base URL of the identity provider's API (`PS_AUTHENTIK_BASE_URL`);
            `None` when unset.
        authentik_public_url: Base of the invitee-facing enrollment link
            (`PS_AUTHENTIK_PUBLIC_URL`); `None` when unset, in which case the API base URL is
            used. The API call itself always uses `authentik_base_url`.
    """

    host: str
    port: int
    graceful_shutdown_seconds: int
    logging_dir: Path | None
    llm_interface_model: str | None = None
    llm_interface_embed_model: str | None = None
    falkordb_host: str = _DEFAULT_FALKORDB_HOST
    falkordb_port: int = _DEFAULT_FALKORDB_PORT
    falkordb_graph: str = _DEFAULT_FALKORDB_GRAPH
    company_merge_capability_merge_threshold: float | None = None
    company_merge_capability_review_threshold: float | None = None
    company_merge_role_review_threshold: float | None = None
    is_local_test_bypass_active: bool = False
    max_request_body_bytes: int = _DEFAULT_MAX_REQUEST_BODY_BYTES
    query_timeout_ms: int = _DEFAULT_QUERY_TIMEOUT_MS
    query_row_cap: int = _DEFAULT_QUERY_ROW_CAP
    falkordb_connect_timeout_seconds: float = _DEFAULT_FALKORDB_CONNECT_TIMEOUT_SECONDS
    falkordb_socket_timeout_seconds: float = _DEFAULT_FALKORDB_SOCKET_TIMEOUT_SECONDS
    state_postgres_connect_timeout_seconds: float = _DEFAULT_STATE_POSTGRES_CONNECT_TIMEOUT_SECONDS
    state_postgres_statement_timeout_ms: int = _DEFAULT_STATE_POSTGRES_STATEMENT_TIMEOUT_MS
    passkey_signing_postgres_connect_timeout_seconds: float = (
        _DEFAULT_PASSKEY_SIGNING_POSTGRES_CONNECT_TIMEOUT_SECONDS
    )
    passkey_signing_postgres_statement_timeout_ms: int = (
        _DEFAULT_PASSKEY_SIGNING_POSTGRES_STATEMENT_TIMEOUT_MS
    )
    graph_drift_check_interval_seconds: int = _DEFAULT_GRAPH_DRIFT_CHECK_INTERVAL_SECONDS
    ingestion_runs_max_in_flight: int = _DEFAULT_INGESTION_RUNS_MAX_IN_FLIGHT
    auth_issuer: str | None = None
    auth_audience: str | None = None
    auth_discovery_retry_budget_seconds: int = _DEFAULT_AUTH_DISCOVERY_RETRY_BUDGET_SECONDS
    auth_cli_client_id: str | None = None
    auth_scopes: tuple[str, ...] = ()
    curated_source_base_url: str = _DEFAULT_CURATED_SOURCE_BASE_URL
    curated_source_allow_insecure_http: bool = False
    passkey_signing_postgres_host: str | None = None
    passkey_signing_postgres_port: int = _DEFAULT_PASSKEY_SIGNING_POSTGRES_PORT
    passkey_signing_postgres_database: str | None = None
    passkey_signing_postgres_user: str | None = None
    passkey_signing_postgres_password: str | None = field(default=None, repr=False)
    passkey_signing_allowed_origins: tuple[str, ...] = ()
    public_url: str | None = None
    state_postgres_host: str | None = None
    state_postgres_port: int = _DEFAULT_STATE_POSTGRES_PORT
    state_postgres_database: str | None = None
    state_postgres_user: str | None = None
    state_postgres_password: str | None = field(default=None, repr=False)
    authz_bootstrap_owner_subject: str | None = None
    authz_bootstrap_owner_issuer: str | None = None
    authentik_api_token: str | None = field(default=None, repr=False)
    authentik_base_url: str | None = field(default=None, repr=False)
    authentik_public_url: str | None = None


# The `ServiceConfig` fields the ingestion pipeline (Domain Mapper, Company Merge) cannot run
# without, but that `load_config()` itself leaves optional (see `ServiceConfig`). Named once
# here so both the request-time guard (`ingestion_orchestration._require_ingestion_config`)
# and the `/ready` startup check stay in lockstep on exactly which four fields that is.
INGESTION_REQUIRED_CONFIG_FIELDS = (
    "llm_interface_model",
    "llm_interface_embed_model",
    "company_merge_capability_merge_threshold",
    "company_merge_capability_review_threshold",
    "company_merge_role_review_threshold",
)


def missing_ingestion_config_fields(config: ServiceConfig) -> list[str]:
    """List the ingestion-required config fields that are unset on `config`.

    Args:
        config: The resolved process configuration to inspect.

    Returns:
        The names from `INGESTION_REQUIRED_CONFIG_FIELDS` whose value on `config` is `None`, in
        that tuple's order; empty when the ingestion pipeline's configuration is complete.
    """
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
    """Parse and validate `PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS` (a non-negative integer).

    A malformed value fails closed with the same typed error as the host/port checks rather
    than raising a raw `ValueError`.
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


def _parse_falkordb_graph(raw: str) -> str:
    """Validate `PS_FALKORDB_GRAPH`, rejecting an explicitly-set empty/whitespace-only value.

    The error names only the variable: no value, host or port is included.

    Args:
        raw: The variable's value as set in the environment.

    Returns:
        `raw`, unchanged.

    Raises:
        ServiceConfigurationError: `raw` is empty or whitespace-only.
    """
    if not raw.strip():
        message = "PS_FALKORDB_GRAPH must not be empty or whitespace-only"
        raise ServiceConfigurationError(message)
    return raw


def _parse_merge_threshold(raw: str) -> float:
    """Parse `PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD`, failing closed on any invalid value.

    Mirrors `_parse_falkordb_port`'s exact validation shape (parse, then
    range-check, raising `ServiceConfigurationError` naming the env var and
    the invalid value on either failure). Only ever called when the env var
    is actually set — absence is not an error at this layer: the
    "fail closed on missing" requirement is enforced by
    `merge_baseline_graph`'s own call site, because `load_config()`
    cannot know whether Company Merge will be used in a given
    process, and every other caller of `load_config()`
    must keep working with this env var unset.
    """
    try:
        threshold = float(raw)
    except ValueError as exc:
        message = f"PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD must be a float, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if not (0.0 < threshold <= 1.0):
        message = (
            "PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD must be greater than 0.0 and at "
            f"most 1.0, got {threshold}"
        )
        raise ServiceConfigurationError(message)
    return threshold


def _parse_review_threshold(raw: str, variable: str) -> float:
    """Parse a review-threshold environment variable, failing closed on any invalid value.

    Unlike the merge threshold, `0.0` is valid: it is the floor that hides nothing. NaN passes
    every comparison as false, so it is rejected explicitly. Only called when the env var is set.

    Args:
        raw: The raw environment value.
        variable: The environment variable name, used in the error messages.

    Returns:
        The threshold, between 0.0 and 1.0 inclusive.

    Raises:
        ServiceConfigurationError: `raw` is not a float, or is outside 0.0 to 1.0 inclusive.
    """
    try:
        threshold = float(raw)
    except ValueError as exc:
        message = f"{variable} must be a float, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if math.isnan(threshold) or not (0.0 <= threshold <= 1.0):
        message = f"{variable} must be at least 0.0 and at most 1.0, got {threshold}"
        raise ServiceConfigurationError(message)
    return threshold


_CAPABILITY_REVIEW_VARIABLE = "PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD"
_ROLE_REVIEW_VARIABLE = "PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD"


def _load_capability_thresholds() -> tuple[float | None, float | None]:
    """Read and validate the Capability merge and review thresholds from the environment.

    Each is `None` when its variable is unset. When both are set, the review threshold must not
    exceed the merge threshold.

    Returns:
        The `(merge, review)` thresholds.

    Raises:
        ServiceConfigurationError: A value is invalid, or review is above merge.
    """
    merge_raw = os.environ.get("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD")
    review_raw = os.environ.get(_CAPABILITY_REVIEW_VARIABLE)
    merge = _parse_merge_threshold(merge_raw) if merge_raw is not None else None
    review = (
        _parse_review_threshold(review_raw, _CAPABILITY_REVIEW_VARIABLE)
        if review_raw is not None
        else None
    )
    if merge is not None and review is not None and review > merge:
        message = (
            f"PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD ({review}) must not exceed "
            f"PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD ({merge})"
        )
        raise ServiceConfigurationError(message)
    return merge, review


def _load_role_review_threshold() -> float | None:
    """Read and validate the Role review threshold from the environment.

    Returns:
        The threshold, or `None` when `PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD` is unset.

    Raises:
        ServiceConfigurationError: The value is invalid.
    """
    raw = os.environ.get(_ROLE_REVIEW_VARIABLE)
    return _parse_review_threshold(raw, _ROLE_REVIEW_VARIABLE) if raw is not None else None


def _parse_max_request_body_bytes(raw: str) -> int:
    """Parse and range-check `PS_SERVICE_MAX_REQUEST_BODY_BYTES`, failing closed on any bad value.

    The limit is enforced by `_MaxBodySizeMiddleware` (`main.py`) against
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


def _parse_auth_discovery_retry_budget_seconds(raw: str) -> int:
    """Parse and range-check `PS_AUTH_DISCOVERY_RETRY_BUDGET_SECONDS`, failing closed if bad.

    Zero is valid and means a single discovery attempt with no retry; a negative or non-integer
    value is rejected, with a message naming this env var.
    """
    try:
        budget_seconds = int(raw)
    except ValueError as exc:
        message = f"PS_AUTH_DISCOVERY_RETRY_BUDGET_SECONDS must be an integer, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if budget_seconds < 0:
        message = (
            f"PS_AUTH_DISCOVERY_RETRY_BUDGET_SECONDS must not be negative, got {budget_seconds}"
        )
        raise ServiceConfigurationError(message)
    return budget_seconds


def _load_auth_discovery_retry_budget_seconds() -> int:
    """Read `PS_AUTH_DISCOVERY_RETRY_BUDGET_SECONDS` from the environment, defaulting when unset.

    Raises:
        ServiceConfigurationError: The value is not a non-negative integer.
    """
    return _parse_auth_discovery_retry_budget_seconds(
        os.environ.get(
            "PS_AUTH_DISCOVERY_RETRY_BUDGET_SECONDS",
            str(_DEFAULT_AUTH_DISCOVERY_RETRY_BUDGET_SECONDS),
        )
    )


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


def _parse_positive_seconds(raw: str, *, env_var_name: str) -> float:
    """Parse a duration in seconds, failing closed on any bad value.

    Args:
        raw: The raw environment value.
        env_var_name: Variable name, quoted in the error message.

    Returns:
        The duration in seconds; always finite and greater than zero.

    Raises:
        ServiceConfigurationError: The value is not a number, is not finite, or is not positive.
    """
    try:
        seconds = float(raw)
    except ValueError as exc:
        message = f"{env_var_name} must be a number of seconds, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if not math.isfinite(seconds) or seconds <= 0:
        message = f"{env_var_name} must be a positive finite number of seconds, got {raw!r}"
        raise ServiceConfigurationError(message)
    return seconds


def _parse_positive_milliseconds(raw: str, *, env_var_name: str) -> int:
    """Parse a duration in whole milliseconds, failing closed on any bad value.

    Args:
        raw: The raw environment value.
        env_var_name: Variable name, quoted in the error message.

    Returns:
        The duration in milliseconds; always greater than zero.

    Raises:
        ServiceConfigurationError: The value is not an integer or is not positive.
    """
    try:
        milliseconds = int(raw)
    except ValueError as exc:
        message = f"{env_var_name} must be an integer number of milliseconds, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if milliseconds <= 0:
        message = f"{env_var_name} must be positive, got {milliseconds}"
        raise ServiceConfigurationError(message)
    return milliseconds


def _require_socket_timeout_above_query_timeout(
    socket_timeout_seconds: float, query_timeout_ms: int
) -> None:
    """Reject a FalkorDB socket timeout that would cut off a healthy slow query.

    Args:
        socket_timeout_seconds: `PS_FALKORDB_SOCKET_TIMEOUT_SECONDS`, in seconds.
        query_timeout_ms: `PS_QUERY_TIMEOUT_MS`, in milliseconds.

    Raises:
        ServiceConfigurationError: The socket timeout does not exceed the query timeout.
    """
    if socket_timeout_seconds * 1000 <= query_timeout_ms:
        message = (
            f"PS_FALKORDB_SOCKET_TIMEOUT_SECONDS ({socket_timeout_seconds} s) must be greater "
            f"than PS_QUERY_TIMEOUT_MS ({query_timeout_ms} ms)"
        )
        raise ServiceConfigurationError(message)


@dataclass(frozen=True)
class _ConnectionBounds:
    """The six connect, socket and statement timeouts read from the environment."""

    falkordb_connect_timeout_seconds: float
    falkordb_socket_timeout_seconds: float
    state_postgres_connect_timeout_seconds: float
    state_postgres_statement_timeout_ms: int
    passkey_signing_postgres_connect_timeout_seconds: float
    passkey_signing_postgres_statement_timeout_ms: int


def _load_connection_bounds(*, query_timeout_ms: int) -> _ConnectionBounds:
    """Read and validate the six connection-bound settings from the environment.

    Args:
        query_timeout_ms: The validated `PS_QUERY_TIMEOUT_MS`, which the FalkorDB socket timeout
            must exceed.

    Returns:
        The validated settings, defaulted where unset.

    Raises:
        ServiceConfigurationError: A value is invalid, or the socket timeout does not exceed the
            query timeout.
    """

    def seconds(name: str, default: float) -> float:
        return _parse_positive_seconds(os.environ.get(name, str(default)), env_var_name=name)

    def milliseconds(name: str, default: int) -> int:
        return _parse_positive_milliseconds(os.environ.get(name, str(default)), env_var_name=name)

    bounds = _ConnectionBounds(
        falkordb_connect_timeout_seconds=seconds(
            "PS_FALKORDB_CONNECT_TIMEOUT_SECONDS", _DEFAULT_FALKORDB_CONNECT_TIMEOUT_SECONDS
        ),
        falkordb_socket_timeout_seconds=seconds(
            "PS_FALKORDB_SOCKET_TIMEOUT_SECONDS", _DEFAULT_FALKORDB_SOCKET_TIMEOUT_SECONDS
        ),
        state_postgres_connect_timeout_seconds=seconds(
            "PS_STATE_POSTGRES_CONNECT_TIMEOUT_SECONDS",
            _DEFAULT_STATE_POSTGRES_CONNECT_TIMEOUT_SECONDS,
        ),
        state_postgres_statement_timeout_ms=milliseconds(
            "PS_STATE_POSTGRES_STATEMENT_TIMEOUT_MS",
            _DEFAULT_STATE_POSTGRES_STATEMENT_TIMEOUT_MS,
        ),
        passkey_signing_postgres_connect_timeout_seconds=seconds(
            "PS_PASSKEYSIGNING_POSTGRES_CONNECT_TIMEOUT_SECONDS",
            _DEFAULT_PASSKEY_SIGNING_POSTGRES_CONNECT_TIMEOUT_SECONDS,
        ),
        passkey_signing_postgres_statement_timeout_ms=milliseconds(
            "PS_PASSKEYSIGNING_POSTGRES_STATEMENT_TIMEOUT_MS",
            _DEFAULT_PASSKEY_SIGNING_POSTGRES_STATEMENT_TIMEOUT_MS,
        ),
    )
    _require_socket_timeout_above_query_timeout(
        bounds.falkordb_socket_timeout_seconds, query_timeout_ms
    )
    return bounds


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


def _parse_graph_drift_check_interval_seconds(raw: str) -> int:
    """Parse `PS_GRAPHGATEWAY_DRIFT_CHECK_INTERVAL_SECONDS`, failing closed on any bad value.

    The pause between two passes of the Graph Write Gateway's drift check. Zero is
    valid and turns the periodic check off; a negative or non-integer value is refused. Never
    required: the default is one hour.
    """
    try:
        interval = int(raw)
    except ValueError as exc:
        message = f"PS_GRAPHGATEWAY_DRIFT_CHECK_INTERVAL_SECONDS must be an integer, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if interval < 0:
        message = (
            f"PS_GRAPHGATEWAY_DRIFT_CHECK_INTERVAL_SECONDS must not be negative, got {interval}"
        )
        raise ServiceConfigurationError(message)
    return interval


def _parse_ingestion_runs_max_in_flight(raw: str) -> int:
    """Parse and range-check `PS_INGESTIONRUNS_MAX_IN_FLIGHT`, failing closed on any bad value.

    The most ingestion runs `start-ingestion` lets execute concurrently in this process; a
    submission over the cap is rejected with a named rate-limit error. Same validation shape
    as `_parse_query_row_cap` (parse, then positivity check).
    """
    try:
        max_in_flight = int(raw)
    except ValueError as exc:
        message = f"PS_INGESTIONRUNS_MAX_IN_FLIGHT must be an integer, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if max_in_flight <= 0:
        message = f"PS_INGESTIONRUNS_MAX_IN_FLIGHT must be positive, got {max_in_flight}"
        raise ServiceConfigurationError(message)
    return max_in_flight


def _parse_local_test_bypass(raw: str | None) -> bool:
    """Parse `PS_SERVICE_LOCAL_TEST_BYPASS`, failing closed on any unrecognized value.

    Unset or empty/whitespace-only resolves to inactive (`False`)
    rather than raising — an operator who never set the var, or set it to
    `""`, gets silence, not a crash. Only `"true"`/`"false"` (case-insensitive)
    are recognized; anything else fails config loading closed
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
    whitespace-only resolves to `False` (TLS required by default)
    rather than raising. Only `"true"`/`"false"` (case-insensitive) are
    recognized; anything else fails config loading closed.
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
    """Validate `PS_CURATEDSOURCE_URL` via the shared http(s)/TLS guard.

    Delegates to `curated_source.source_url.validate_source_url` -- the same
    function the runtime `set-catalog-source` MCP tool validates a
    new override URL through -- and translates its
    `CuratedSourceConfigurationError` into `ServiceConfigurationError` so
    this layer's failure type stays uniform with every other `load_config()`
    validation failure.
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
    raw value in its error message: a validation failure here
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
    it as unset. Only checks non-empty -- unlike `curated_source_base_url`, no URL
    scheme or shape validation is applied to this field.
    """
    if not raw.strip():
        message = "PS_AUTHENTIK_BASE_URL must not be empty or whitespace-only"
        raise ServiceConfigurationError(message)
    return raw


def _parse_authentik_public_url(raw: str) -> str:
    """Validate `PS_AUTHENTIK_PUBLIC_URL`.

    Same rule as `_parse_authentik_base_url`: an explicitly-set empty or
    whitespace-only value is rejected rather than treated as unset.
    """
    if not raw.strip():
        message = "PS_AUTHENTIK_PUBLIC_URL must not be empty or whitespace-only"
        raise ServiceConfigurationError(message)
    return raw


def _resolve_authentik_api_token() -> str | None:
    """Resolve `PS_AUTHENTIK_API_TOKEN`, validating it if set.

    Factored out of `load_config()` (unlike every other optional
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


def _resolve_authentik_public_url() -> str | None:
    """Resolve `PS_AUTHENTIK_PUBLIC_URL`, validating it if set."""
    raw = os.environ.get("PS_AUTHENTIK_PUBLIC_URL")
    return _parse_authentik_public_url(raw) if raw is not None else None


def _parse_auth_scopes(raw: str) -> tuple[str, ...]:
    """Parse `PS_AUTH_SCOPES` into an informational-only tuple of scope names.

    Split on commas and whitespace, discarding empty entries -- never
    enforced as authorization (only the token audience is checked).
    Only called when the env var is actually set; unset resolves to `()` in
    `load_config()` directly, without calling this function.
    """
    return tuple(part for part in raw.replace(",", " ").split() if part)


def _parse_passkey_signing_allowed_origins(raw: str) -> tuple[str, ...]:
    """Parse `PS_PASSKEYSIGNING_ALLOWED_ORIGINS` into an ordered tuple of origins.

    Each comma-separated entry is trimmed and must be `http://` or `https://` followed by a
    host and an optional port: no path (a trailing slash included), query, fragment or user
    info. An explicitly set value that is empty or has an empty entry is rejected rather than
    treated as unset. The error names the variable, never the offending value.
    """
    origins = tuple(part.strip() for part in raw.split(","))
    if not all(_is_plain_origin(origin) for origin in origins):
        message = (
            "PS_PASSKEYSIGNING_ALLOWED_ORIGINS must be a comma-separated list of "
            "http(s)://host[:port] origins with no path, query or fragment"
        )
        raise ServiceConfigurationError(message)
    return origins


def _resolve_passkey_signing_allowed_origins() -> tuple[str, ...]:
    """Resolve `PS_PASSKEYSIGNING_ALLOWED_ORIGINS`: parsed when set, `()` when unset."""
    raw = os.environ.get("PS_PASSKEYSIGNING_ALLOWED_ORIGINS")
    return _parse_passkey_signing_allowed_origins(raw) if raw is not None else ()


def _parse_public_url(raw: str | None, *, is_local_test_bypass_active: bool) -> str | None:
    """Parse `PS_PUBLIC_URL`: `None` when unset, else a validated `https://host[:port]` origin.

    An explicitly set value that is empty, not an absolute http(s) origin, carries a path (a
    trailing slash included), query, fragment or user info, or uses `http` outside local-test
    mode is rejected rather than treated as unset. The error names the variable, never the
    offending value.
    """
    if raw is None:
        return None
    origin = raw.strip()
    if not _is_plain_origin(origin):
        message = (
            "PS_PUBLIC_URL must be an absolute origin of the form https://host[:port] "
            "with no path, query or fragment"
        )
        raise ServiceConfigurationError(message)
    if urlsplit(origin).scheme != "https" and not is_local_test_bypass_active:
        message = "PS_PUBLIC_URL must use https unless PS_SERVICE_LOCAL_TEST_BYPASS is true"
        raise ServiceConfigurationError(message)
    return origin


def _is_plain_origin(origin: str) -> bool:
    """Return whether `origin` is exactly `http(s)://host[:port]`."""
    try:
        parts = urlsplit(origin)
        _ = parts.port
    except ValueError:
        return False
    return (
        parts.scheme in {"http", "https"}
        and bool(parts.hostname)
        and "@" not in parts.netloc
        and origin == f"{parts.scheme}://{parts.netloc}"
    )


def load_config() -> ServiceConfig:
    """Resolve the process environment into a validated `ServiceConfig`.

    Reads the `PS_*` environment variables once and validates each that is set. Unset
    variables take the field defaults documented on `ServiceConfig`; optional fields without a
    default resolve to `None` (or an empty tuple), because requiredness is enforced by the
    consuming component, not here. The result is immutable.

    Returns:
        The fully resolved configuration.

    Raises:
        ServiceConfigurationError: any set variable is malformed, out of range, blank where a
            value is required, or (for the curated-content source URL) fails the shared
            http(s)/TLS guard. Raised before any other part of the configuration is used.

    Side Effects:
        Reads `os.environ`; nothing is written.
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

    company_merge_capability_merge_threshold, company_merge_capability_review_threshold = (
        _load_capability_thresholds()
    )
    company_merge_role_review_threshold = _load_role_review_threshold()

    is_local_test_bypass_active = _parse_local_test_bypass(
        os.environ.get("PS_SERVICE_LOCAL_TEST_BYPASS")
    )

    max_request_body_bytes = _parse_max_request_body_bytes(
        os.environ.get("PS_SERVICE_MAX_REQUEST_BODY_BYTES", str(_DEFAULT_MAX_REQUEST_BODY_BYTES))
    )

    query_timeout_ms = _parse_query_timeout_ms(
        os.environ.get("PS_QUERY_TIMEOUT_MS", str(_DEFAULT_QUERY_TIMEOUT_MS))
    )
    connection_bounds = _load_connection_bounds(query_timeout_ms=query_timeout_ms)

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
        falkordb_graph=_parse_falkordb_graph(
            os.environ.get("PS_FALKORDB_GRAPH", _DEFAULT_FALKORDB_GRAPH)
        ),
        company_merge_capability_merge_threshold=company_merge_capability_merge_threshold,
        company_merge_capability_review_threshold=company_merge_capability_review_threshold,
        company_merge_role_review_threshold=company_merge_role_review_threshold,
        is_local_test_bypass_active=is_local_test_bypass_active,
        max_request_body_bytes=max_request_body_bytes,
        query_timeout_ms=query_timeout_ms,
        query_row_cap=_parse_query_row_cap(
            os.environ.get("PS_QUERY_ROW_CAP", str(_DEFAULT_QUERY_ROW_CAP))
        ),
        falkordb_connect_timeout_seconds=connection_bounds.falkordb_connect_timeout_seconds,
        falkordb_socket_timeout_seconds=connection_bounds.falkordb_socket_timeout_seconds,
        state_postgres_connect_timeout_seconds=(
            connection_bounds.state_postgres_connect_timeout_seconds
        ),
        state_postgres_statement_timeout_ms=connection_bounds.state_postgres_statement_timeout_ms,
        passkey_signing_postgres_connect_timeout_seconds=(
            connection_bounds.passkey_signing_postgres_connect_timeout_seconds
        ),
        passkey_signing_postgres_statement_timeout_ms=(
            connection_bounds.passkey_signing_postgres_statement_timeout_ms
        ),
        graph_drift_check_interval_seconds=_parse_graph_drift_check_interval_seconds(
            os.environ.get(
                "PS_GRAPHGATEWAY_DRIFT_CHECK_INTERVAL_SECONDS",
                str(_DEFAULT_GRAPH_DRIFT_CHECK_INTERVAL_SECONDS),
            )
        ),
        ingestion_runs_max_in_flight=_parse_ingestion_runs_max_in_flight(
            os.environ.get(
                "PS_INGESTIONRUNS_MAX_IN_FLIGHT", str(_DEFAULT_INGESTION_RUNS_MAX_IN_FLIGHT)
            )
        ),
        auth_issuer=auth_issuer,
        auth_audience=auth_audience,
        auth_discovery_retry_budget_seconds=_load_auth_discovery_retry_budget_seconds(),
        auth_cli_client_id=auth_cli_client_id,
        auth_scopes=auth_scopes,
        curated_source_base_url=curated_source_base_url,
        curated_source_allow_insecure_http=curated_source_allow_insecure_http,
        passkey_signing_postgres_host=passkey_signing_postgres_host,
        passkey_signing_postgres_port=passkey_signing_postgres_port,
        passkey_signing_postgres_database=passkey_signing_postgres_database,
        passkey_signing_postgres_user=passkey_signing_postgres_user,
        passkey_signing_postgres_password=passkey_signing_postgres_password,
        passkey_signing_allowed_origins=_resolve_passkey_signing_allowed_origins(),
        public_url=_parse_public_url(
            os.environ.get("PS_PUBLIC_URL"),
            is_local_test_bypass_active=is_local_test_bypass_active,
        ),
        state_postgres_host=state_postgres_host,
        state_postgres_port=state_postgres_port,
        state_postgres_database=state_postgres_database,
        state_postgres_user=state_postgres_user,
        state_postgres_password=state_postgres_password,
        authz_bootstrap_owner_subject=authz_bootstrap_owner_subject,
        authz_bootstrap_owner_issuer=authz_bootstrap_owner_issuer,
        authentik_api_token=_resolve_authentik_api_token(),
        authentik_base_url=_resolve_authentik_base_url(),
        authentik_public_url=_resolve_authentik_public_url(),
    )
