"""Catalog, restore and invite tools of the PS MCP server.

Declares `set-catalog-source`, `reset-catalog-source`, `get-catalog-source`, `invite-user`,
`get-catalog-listing` and `restore-instrument`; `register_catalog_tools` registers them on a server
built by `mcp_server.build_server`. The tools build their runtime-config and audit stores through
the `GateEnvironment` they are bound to. This module imports only `tool_core` and `tool_gates`
(never another tool group); `create_invitation` and `build_default_curated_catalog_dependencies`
are bound here and patched here by tests.

Business Rules:
    - Catalog-source administration and `invite-user` need `SystemAdmin` or above; the catalog
      listing is a plain read; `restore-instrument` needs `ComplianceOfficer`.
    - A curated instrument id is validated by charset and length, and rejected when it contains
      `..`, before the tool body runs.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Annotated

from pydantic import AfterValidator, Field

from ps_service.api.errors import (
    CatalogSourceOverrideUnavailableError,
    CuratedSourceUnavailableError,
    GraphWriteUnavailableError,
    RestoreArtifactRejectedError,
    RestoreInstrumentIdAmbiguousError,
    RestoreInstrumentIdNotFoundError,
    RestoreStageFailedError,
)
from ps_service.api.models import (
    CatalogInstrumentEntry,
    CatalogRestorationRequest,
    CuratedCatalogResponse,
)
from ps_service.api.restore_orchestration import (
    build_default_restore_from_catalog_dependencies,
    run_restoration_from_catalog_source,
)
from ps_service.audit.errors import AuditTrailUnavailableError
from ps_service.config import load_config
from ps_service.curated_source import store as catalog_source_store
from ps_service.curated_source.catalog_client import build_default_curated_catalog_dependencies
from ps_service.curated_source.errors import CuratedSourceFetchError
from ps_service.curated_source.resolve import resolve_effective_source
from ps_service.invitations.client import create_invitation
from ps_service.invitations.errors import AuthentikInvitationError
from ps_service.invitations.service import invite_user_audited
from ps_service.mcp_interface.errors import McpGraphUnavailableError
from ps_service.mcp_interface.tool_core import COMPLIANCE_OFFICER_GATE as _COMPLIANCE_OFFICER_GATE
from ps_service.mcp_interface.tool_core import (
    GRAPH_UNAVAILABLE_MESSAGE as _GRAPH_UNAVAILABLE_MESSAGE,
)
from ps_service.mcp_interface.tool_core import MCP_CALLER_LABEL as _MCP_CALLER_LABEL
from ps_service.mcp_interface.tool_core import PLAIN_READ_GATE as _PLAIN_READ_GATE
from ps_service.mcp_interface.tool_core import SYSTEM_ADMIN_GATE as _SYSTEM_ADMIN_GATE
from ps_service.mcp_interface.tool_core import audit_context as _audit_context
from ps_service.mcp_interface.tool_core import declared_tool as _gated_tool
from ps_service.mcp_interface.tool_core import resolve_authz_actor as _resolve_authz_actor
from ps_service.mcp_interface.tool_core import (
    resolve_policy_lifecycle_actor as _resolve_policy_lifecycle_actor,
)
from ps_service.mcp_interface.tool_core import run_mcp_action as _run_mcp_action
from ps_service.mcp_interface.tool_core import sanitize_graph_open as _sanitize_graph_open
from ps_service.mcp_interface.tool_gates import GateEnvironment, register_declared_tools
from ps_service.runtime_config import (
    RuntimeConfigError,
    RuntimeConfigInvalidValueError,
    RuntimeConfigPersistenceError,
    RuntimeConfigUnavailableError,
)

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from ps_service.api.restore_orchestration import CatalogRestoreDependencies
    from ps_service.curated_source.catalog_client import CuratedCatalogDependencies
    from ps_service.mcp_interface.tool_gates import ToolGate


# Charset and length bound for a curated instrument id, the same as ps-cli's own instrument-id
# pattern and at least as strict as `CatalogRestorationRequest.instrument_id`'s REST pattern,
# which has no `..` guard of its own. The `..` rejection is a separate `AfterValidator`
# (`_reject_path_traversal_segment`) rather than a look-ahead in this regex, because
# pydantic-core's regex backend (the Rust `regex` crate) does not support look-around and raises
# a `SchemaError` at tool-registration time. The validator still runs inside pydantic's
# schema-validation pass, before the tool body, mirroring ps-cli's regex-then-`..` two-step check.
_RESTORE_INSTRUMENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
# A deliberately permissive structural "looks like an email" check for `invite_user`. Full
# RFC 5322 validation is out of scope: `pydantic.EmailStr` needs the `email-validator` package,
# which is not an installed dependency, and the real validation authority is Authentik itself
# at redemption time.
_INVITE_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def _reject_path_traversal_segment(value: str) -> str:
    """Reject an instrument id that contains `..`.

    Runs as an `AfterValidator` once `_RESTORE_INSTRUMENT_ID_PATTERN` has matched, inside pydantic's
    schema-validation pass, so the check happens before `restore-instrument`'s body runs. It is a
    separate validator because the regex backend cannot express the rule.

    Args:
        value: An instrument id that already matched the charset and length pattern.

    Returns:
        `value`, unchanged.

    Raises:
        ValueError: `value` contains `..`.
    """
    if ".." in value:
        msg = "instrument_id must not contain '..'"
        raise ValueError(msg)
    return value


def _sanitize_restore_graph_opens(
    dependencies: CatalogRestoreDependencies,
) -> CatalogRestoreDependencies:
    """Return restore `dependencies` with `open_db` wrapped by `_sanitize_graph_open`.

    `run_restoration_from_catalog_source` calls `open_db` directly with no error handling of its
    own.
    """
    return dataclasses.replace(dependencies, open_db=_sanitize_graph_open(dependencies.open_db))


# Fixed, detail-free string for the catalog-source tools' read failures: never host, port,
# driver text or the (possibly credential-bearing) override URL.
_RUNTIME_CONFIG_UNAVAILABLE_MESSAGE = (
    "error: The runtime configuration store is temporarily unavailable."
)


@_gated_tool("set-catalog-source", _SYSTEM_ADMIN_GATE)
def set_catalog_source(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment, url: Annotated[str, Field(min_length=1)]
) -> dict[str, object] | str:
    """Override the curated-content source used for catalog listings and artifact fetches.

    Validates `url` with the same rules startup configuration applies: only `http(s)://` schemes are
    accepted, and a plain `http://` URL is rejected unless `PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP` is
    set for the service process. The override takes effect at once, with no restart, and takes
    precedence over `PS_CURATEDSOURCE_URL` and the public default for every later catalog listing
    and artifact fetch until `reset-catalog-source` is called. Requires the SystemAdmin role or
    above (skipped only under the local-test bypass).

    Args:
        url: Base URL of the curated-content source; at least 1 character, `http(s)://` only.

    Returns:
        `{"url": <the validated url>, "source": "override"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller is not authenticated or lacks the required access role.
        - `url` fails validation (the message says why).
        - The runtime configuration store cannot be reached or the write could not be applied;
          nothing is changed.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Persists `url` as the override in the PS state database and writes one audit event for the
        change in the same transaction.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        audit = _audit_context(config, actor, environment=environment)
        if isinstance(audit, str):
            return audit
        try:
            catalog_source_store.set_override(
                environment.runtime_config_store(config),
                url,
                actor=audit.actor,
            )
        except (
            RuntimeConfigInvalidValueError,
            RuntimeConfigUnavailableError,
            RuntimeConfigPersistenceError,
        ) as exc:
            # Either the validator's own message or a fixed store message.
            return f"error: {exc}"
        return {"url": url, "source": "override"}

    return _run_mcp_action("set_catalog_source", _body)


@_gated_tool("reset-catalog-source", _SYSTEM_ADMIN_GATE)
def reset_catalog_source(environment: GateEnvironment) -> dict[str, object] | str:
    """Clear the persisted curated-content source override.

    The effective source immediately reverts to `PS_CURATEDSOURCE_URL` or the public default for
    every later catalog listing and artifact fetch, with no restart. Takes no parameters. Requires
    the SystemAdmin role or above (skipped only under the local-test bypass).

    Returns:
        `{"url": <the configured default url>, "source": "default"}`. Resetting when no override is
        set changes nothing and is not an error.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller is not authenticated or lacks the required access role.
        - The runtime configuration store cannot be reached or the write could not be applied;
          nothing is changed.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Deletes the persisted override from the PS state database, if any, and writes one audit
        event in the same transaction.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        audit = _audit_context(config, actor, environment=environment)
        if isinstance(audit, str):
            return audit
        try:
            catalog_source_store.reset_override(
                environment.runtime_config_store(config),
                actor=audit.actor,
            )
        except (RuntimeConfigUnavailableError, RuntimeConfigPersistenceError) as exc:
            return f"error: {exc}"
        return {"url": config.curated_source_base_url, "source": "default"}

    return _run_mcp_action("reset_catalog_source", _body)


@_gated_tool("get-catalog-source", _SYSTEM_ADMIN_GATE)
def get_catalog_source(environment: GateEnvironment) -> dict[str, object] | str:
    """Report the currently effective curated-content source.

    Looks for a persisted override in the PS state database first and falls back to
    `PS_CURATEDSOURCE_URL` or the public default only when the store answered and holds no override.
    It fails closed: when the store cannot be read this tool returns an error and never reports the
    default source, because the operator's override may point elsewhere on purpose. Takes no
    parameters. Requires the SystemAdmin role or above (skipped only under the local-test bypass).

    Returns:
        `{"url": <the effective url>, "source": "override"}` when a persisted override is in effect,
        otherwise `{"url": <the default url>, "source": "default"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller is not authenticated or lacks the required access role.
        - The override cannot be read (the runtime configuration store is unavailable).
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        None; the call is read-only and logged.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()

    def _body() -> dict[str, object] | str:
        try:
            effective = resolve_effective_source(
                config,
                store=environment.runtime_config_store(config),
            )
        except RuntimeConfigError:
            return _RUNTIME_CONFIG_UNAVAILABLE_MESSAGE
        return {"url": effective.url, "source": "override" if effective.is_override else "default"}

    return _run_mcp_action("get_catalog_source", _body)


@_gated_tool("invite-user", _SYSTEM_ADMIN_GATE)
def invite_user(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    email: Annotated[str, Field(min_length=3, pattern=_INVITE_EMAIL_PATTERN)],
) -> dict[str, object] | str:
    """Create a single-use Authentik enrollment invite for an email address.

    Uses the service's own configured Authentik service credential, never a caller-supplied token,
    to call Authentik's invitation-stage API. Requires the SystemAdmin role or above (skipped only
    under the local-test bypass).

    Args:
        email: The invitee's email address, at least 3 characters and shaped like `name@domain.tld`.
            Only this plausibility check is made here; Authentik is the real validation authority at
            redemption time.

    Returns:
        `{"itoken": <invitation key>, "invite_url": <redemption URL>}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller is not authenticated or lacks the required access role.
        - Authentik is unreachable or returns a non-2xx response.
        - The audit trail is unavailable: a `user.invite` row is written before Authentik is called,
          and if it cannot be written nothing is sent.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Creates an invitation in Authentik. Writes a `user.invite` audit row before the call, and a
        follow-up row if the call fails.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        audit = _audit_context(config, actor, environment=environment)
        if isinstance(audit, str):
            return audit
        try:
            result = invite_user_audited(
                config, email, audit=audit, send_invitation=create_invitation
            )
        except (AuthentikInvitationError, AuditTrailUnavailableError) as exc:
            return f"error: {exc}"
        return {"itoken": result.itoken, "invite_url": result.invite_url}

    return _run_mcp_action("invite_user", _body)


@_gated_tool("get-catalog-listing", _PLAIN_READ_GATE)
def get_catalog_listing() -> dict[str, object] | str:
    """List every curated instrument, external and internal, in the curated-content source.

    Resolves the effective curated-content source (a persisted override when one exists, else the
    configured default), then fetches and parses its `catalog.json`. It fails closed: when the
    override cannot be read this tool returns an error rather than listing the default source. Takes
    no parameters.

    Returns:
        A dict with an `instruments` list, one entry per curated instrument, unfiltered, each with
        `instrument_id`, `title`, `source_type` and `jurisdiction` (`None` for an internal-source
        entry). Use an `instrument_id` with `restore-instrument`.

    Errors (returned as a string beginning `error: `, never raised):
        - The override cannot be read (the runtime configuration store is unavailable).
        - The curated-content source is unreachable or returns a missing or malformed
          `catalog.json`.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        One network fetch of `catalog.json`; nothing is written. The call is logged.
    """
    config = load_config()

    def _body() -> dict[str, object] | str:
        dependencies: CuratedCatalogDependencies = build_default_curated_catalog_dependencies()
        try:
            effective_source = dependencies.resolve_effective_source(config)
        except RuntimeConfigError:
            return _RUNTIME_CONFIG_UNAVAILABLE_MESSAGE
        try:
            entries = dependencies.fetch_catalog(effective_source.url)
        except CuratedSourceFetchError as exc:
            return f"error: {exc}"
        return CuratedCatalogResponse(
            instruments=[
                CatalogInstrumentEntry(
                    instrument_id=entry.instrument_id,
                    title=entry.title,
                    source_type=entry.source_type,
                    jurisdiction=entry.jurisdiction,
                )
                for entry in entries
            ]
        ).model_dump()

    return _run_mcp_action("get_catalog_listing", _body)


@_gated_tool("restore-instrument", _COMPLIANCE_OFFICER_GATE)
def restore_instrument(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    instrument_id: Annotated[
        str,
        Field(pattern=_RESTORE_INSTRUMENT_ID_PATTERN),
        AfterValidator(_reject_path_traversal_segment),
    ],
) -> dict[str, object] | str:
    """Restore one curated instrument from the curated-content source into the compliance graph.

    Resolves `instrument_id` against the curated catalog without regard to case (`cra-1.0` resolves
    to
    the catalog's own `CRA-1.0`), fetches that instrument's manifest, baseline and native artifact
    from
    the effective curated-content source (a persisted override when one exists, else the configured
    default), verifies the artifact, and restores it into the graph. Requires the ComplianceOfficer
    role
    (skipped only under the local-test bypass). The restoring caller becomes the owner of any draft
    Policy the restore imports.

    Args:
        instrument_id: Catalog id of the instrument, for example `CRA-1.0`; must match the allowed
            id pattern and must not contain `..`. A value that violates either rule is rejected
            before
            the tool runs.

    Returns:
        A dict with `instrument_id`, a `stages` list holding one entry per completed restore
        stage, each with its own `stage` and `status`, and `near_miss_reviews_created`: the
        number of Capability near-miss reviews the restore recorded (resolve them with
        `list-near-misses` and `resolve-near-miss`).

    Errors (returned as a string beginning `error: `, never raised):
        - The caller is not authenticated or lacks the required access role.
        - The audit trail cannot record the opening row; nothing is restored.
        - The persisted source override cannot be read, or the curated-content source is unreachable
          or
          returns a missing or malformed artifact.
        - `instrument_id` matches more than one catalog entry when case is ignored (the message
          names
          every colliding id), or matches no entry (the message names the closest ids); nothing is
          fetched in either case.
        - The artifact fails checksum or `schema_version` verification, or a restore stage fails
          (including a missing `PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD` setting).
        - The policy graph database cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Fetches the catalog and the artifact over the network. Writes an `instrument.restore` audit
        event before the restore starts and a terminal one afterwards. Writes the restored
        instrument,
        and any imported draft Policy owned by the caller, to the policy graph.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    actor = _resolve_authz_actor(config)
    # The restoring caller owns any imported draft Policy. Same resolution as the
    # policy-lifecycle tools (verified `(sub, iss)`, or the local-test bypass identity).
    owner = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        request_body = CatalogRestorationRequest(instrument_id=instrument_id)
        dependencies: CatalogRestoreDependencies = _sanitize_restore_graph_opens(
            build_default_restore_from_catalog_dependencies()
        )
        audit = _audit_context(config, actor, environment=environment)
        if isinstance(audit, str):
            return audit
        try:
            outcome = run_restoration_from_catalog_source(
                request_body,
                config=config,
                actor=_MCP_CALLER_LABEL,
                dependencies=dependencies,
                audit=audit,
                owner=owner,
            )
        except (
            AuditTrailUnavailableError,
            CatalogSourceOverrideUnavailableError,
            CuratedSourceUnavailableError,
            RestoreInstrumentIdAmbiguousError,
            RestoreInstrumentIdNotFoundError,
            GraphWriteUnavailableError,
            RestoreArtifactRejectedError,
            RestoreStageFailedError,
        ) as exc:
            return f"error: {exc}"
        except McpGraphUnavailableError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return outcome.model_dump()

    return _run_mcp_action("restore-instrument", _body)


def register_catalog_tools(
    server: MCPServer, environment: GateEnvironment, gates: dict[str, ToolGate]
) -> None:
    """Register the catalog, restore and invite tools.

    Args:
        server: The server the tools are registered on.
        environment: The server-side collaborators the tools use.
        gates: The registry that records this server's gate declarations.

    Raises:
        ValueError: A tool name is already declared in `gates`.
    """
    register_declared_tools(
        server,
        environment,
        gates,
        (
            set_catalog_source,
            reset_catalog_source,
            get_catalog_source,
            invite_user,
            get_catalog_listing,
            restore_instrument,
        ),
    )
