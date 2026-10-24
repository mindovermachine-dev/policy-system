"""Policy-draft tools of the PS MCP server.

Declares `create-policy-draft`, `update-policy-draft`, `add-standard-to-draft`,
`update-standard-draft`, `add-control-to-draft` and `update-control-draft`;
`register_policy_draft_tools` registers them on a server built by `mcp_server.build_server`.
Each tool delegates to `ps_service.policy_lifecycle.service`, which owns the behaviour, and builds
its graph and stores through the `GateEnvironment` it is bound to. This module imports only
`tool_core` and `tool_gates` (never another tool group).

Business Rules:
    - Draft authoring is gated by the policy-author and draft-access gates.
    - A typed `fields` or `standards` argument is validated against the tool's input schema before
      the tool runs; a malformed one is rejected there and never reaches the tool body. Domain
      failures are returned as `error: <message>` strings, never raised across the MCP boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field

from ps_service.api.errors import (
    GraphWriteUnavailableError,
)
from ps_service.api.graph_write_access import graph_write_access
from ps_service.config import load_config
from ps_service.mcp_interface.draft_input_models import (
    ControlContentFields,
    NewControlContentFields,
    PolicyContentFields,
    PolicyDraftStandardInput,
    StandardContentFields,
)
from ps_service.mcp_interface.errors import McpGraphUnavailableError
from ps_service.mcp_interface.tool_core import POLICY_AUTHOR_GATE as _POLICY_AUTHOR_GATE
from ps_service.mcp_interface.tool_core import POLICY_DRAFT_ACCESS_GATE as _POLICY_DRAFT_ACCESS_GATE
from ps_service.mcp_interface.tool_core import (
    POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE,
)
from ps_service.mcp_interface.tool_core import declared_tool as _gated_tool
from ps_service.mcp_interface.tool_core import resolve_graph as _resolve_graph
from ps_service.mcp_interface.tool_core import (
    resolve_policy_lifecycle_actor as _resolve_policy_lifecycle_actor,
)
from ps_service.mcp_interface.tool_core import run_mcp_action as _run_mcp_action
from ps_service.mcp_interface.tool_gates import (
    GateEnvironment,
    ToolGate,
    register_declared_tools,
)
from ps_service.policy_lifecycle.errors import (
    PolicyCapabilityAlreadyGovernedError,
    PolicyCapabilityNotFoundError,
    PolicyControlNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
    PolicyNotFoundError,
    PolicyStandardNotFoundError,
    PolicySupersedePriorNotApprovedError,
    PolicyTitleAlreadyExistsError,
)
from ps_service.policy_lifecycle.graph_submission import lifecycle_submitter
from ps_service.policy_lifecycle.service import add_control_to_draft as run_add_control_to_draft
from ps_service.policy_lifecycle.service import add_standard_to_draft as run_add_standard_to_draft
from ps_service.policy_lifecycle.service import create_policy_draft as run_create_policy_draft
from ps_service.policy_lifecycle.service import update_control_draft as run_update_control_draft
from ps_service.policy_lifecycle.service import update_policy_draft as run_update_policy_draft
from ps_service.policy_lifecycle.service import update_standard_draft as run_update_standard_draft

if TYPE_CHECKING:
    from mcp.server import MCPServer


_CREATE_POLICY_DRAFT_ERRORS = (
    GraphWriteUnavailableError,
    PolicyCapabilityAlreadyGovernedError,
    PolicyCapabilityNotFoundError,
    PolicyNotFoundError,
    PolicySupersedePriorNotApprovedError,
    PolicyTitleAlreadyExistsError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("create-policy-draft", _POLICY_AUTHOR_GATE)
def create_policy_draft(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    title: Annotated[str, Field(min_length=1)],
    standards: list[PolicyDraftStandardInput] | None = None,
    supersedes_policy_id: Annotated[str, Field(min_length=1)] | None = None,
    capability_ids: list[Annotated[str, Field(min_length=1)]] | None = None,
) -> dict[str, object] | str:
    """Create a new draft Policy owned by the caller, or an amendment of an approved Policy.

    Requires a real authenticated session (the local-test bypass counts as one). The Policy, and
    every
    Standard and Control created with it, starts in `draft` status.

    Args:
        title: Policy title; at least 1 character.
        standards: Optional list of Standards to create with the Policy, each with an optional
            list of Controls (`control_type` `"automated"` or `"manual"`, default `"manual"`). The
            input schema states the shape and rejects unknown keys. Omit it or pass `null` for a
            title-only draft. Ignored when `supersedes_policy_id` is set.
        supersedes_policy_id: Optional id of an `approved` Policy to amend. When omitted this
            creates an
            ordinary version-1 Policy whose id is derived from `title` alone, so repeating a title
            is an
            error. When set, it creates a successor draft whose id derives from both `title` and
            this
            id, with `version` one higher (still a string), a `SUPERSEDED_BY` edge from the prior
            Policy,
            and Standard and Control children forked as new, independently editable copies of the
            prior
            Policy's current children; the prior nodes are not changed. At least 1 character.
        capability_ids: Optional ids of existing Capabilities the new Policy will govern; each at
            least
            1 character. The governing edges are written together with the Policy, so it governs
            them
            from creation, while still a draft. Duplicates are dropped, order kept. Every id must
            name
            an existing Capability that no Policy governs yet. Ignored when `supersedes_policy_id`
            is
            set: a fork's Capabilities stay with the prior Policy and move to the successor when it
                is
            approved.

    Returns:
        `{"policy_id", "title", "status", "version", "owner_subject", "standard_ids", "control_ids",
        "superseded_policy_id", "governed_capability_ids"}`. `standard_ids` and `control_ids` list
        every
        created or forked child (empty for a title-only draft) and can be used in later
        draft-editing
        calls; `superseded_policy_id` is `null` unless this was an amendment;
        `governed_capability_ids`
        is empty when none are governed or on an amendment.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `supersedes_policy_id` names no Policy, or a Policy that is not `approved`.
        - A `capability_ids` entry names no Capability or one that is already governed; nothing is
          written.
        - The computed Policy id already exists.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the Policy and its children to the policy graph in one guarded step and records an
        audit
        event.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            graph = _resolve_graph(config, environment=environment)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        audit_store = environment.audit_store_factory(config)
        try:
            # Fail closed before any read or write: no write path, no lifecycle write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_create_policy_draft(
                actor=actor,
                title=title,
                standards=tuple(standard.to_draft_input() for standard in standards or ()),
                supersedes_policy_id=supersedes_policy_id,
                capability_ids=tuple(capability_ids or ()),
                graph=graph,
                audit_store=audit_store,
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _CREATE_POLICY_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
            "version": result.version,
            "owner_subject": result.owner_subject,
            "standard_ids": list(result.standard_ids),
            "control_ids": list(result.control_ids),
            "superseded_policy_id": result.superseded_policy_id,
            "governed_capability_ids": list(result.capability_ids),
        }

    return _run_mcp_action("create_policy_draft", _body, entity_id=supersedes_policy_id)


_UPDATE_POLICY_DRAFT_ERRORS = (
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("update-policy-draft", _POLICY_DRAFT_ACCESS_GATE)
def update_policy_draft(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    policy_id: Annotated[str, Field(min_length=1)],
    fields: PolicyContentFields | None = None,
) -> dict[str, object] | str:
    """Change a subset of a draft Policy's content fields, leaving the rest as they are.

    Usable only while the Policy is in `draft` status, and only by its owner or a caller holding
    `SystemOwner` or `SystemAdmin`. Requires a real authenticated session (the local-test bypass
    counts as one).

    Args:
        policy_id: Id of the Policy to change; at least 1 character.
        fields: Partial-update map; only the supplied keys change. Each value is a string or JSON
            `null`, which clears the field. Allowed keys: `description`, `scope_in`, `scope_out`,
            `normative_commitments`, `review_cadence`, `exception_pathway`, `measurable_outcomes`,
            `capability_grouping_rationale`. `title`, `status`, `owner_subject`, `owner_issuer` and
            `version` can never be patched. An unknown key or a value that is neither a string nor
            `null` is rejected by the input schema before the tool runs. Omit it or pass `null` for
            a no-op update.

    Returns:
        `{"policy_id", "updated_fields"}`, where `updated_fields` is every key supplied in `fields`,
        sorted.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The caller is neither the owner nor a `SystemOwner` or `SystemAdmin`.
        - The Policy is not in `draft` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the changed fields to the policy graph; no audit event is written. May also backfill
        a missing `version` and `status` on the Policy tree.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            graph = _resolve_graph(config, environment=environment)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        access_role_store = environment.access_role_store(config)
        try:
            # Fail closed before any read or write: no write path, no draft edit.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_update_policy_draft(
                actor=actor,
                policy_id=policy_id,
                fields=fields.supplied() if fields is not None else {},
                graph=graph,
                access_role_store=access_role_store,
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _UPDATE_POLICY_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "updated_fields": list(result.updated_fields),
        }

    return _run_mcp_action("update_policy_draft", _body, entity_id=policy_id)


_ADD_STANDARD_TO_DRAFT_ERRORS = (
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("add-standard-to-draft", _POLICY_DRAFT_ACCESS_GATE)
def add_standard_to_draft(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    policy_id: Annotated[str, Field(min_length=1)],
    title: Annotated[str, Field(min_length=1)],
    fields: StandardContentFields | None = None,
) -> dict[str, object] | str:
    """Add a new Standard under a draft Policy.

    Usable only while the Policy is in `draft` status, and only by its owner or a caller holding
    `SystemOwner` or `SystemAdmin`. Requires a real authenticated session (the local-test bypass
    counts
    as one). The Standard is linked to the Policy with `SUPPORTED_BY` and always starts in `draft`
    governance status, whatever its `implementation_status`.

    Args:
        policy_id: Id of the draft Policy; at least 1 character.
        title: Standard title; at least 1 character.
        fields: Optional content to set at creation. Each value is a string, or JSON `null`, which
            leaves the field unset. Allowed keys: `description`, `implementation_status`,
            `procedure`,
            `implementer_role`, `reviewer_role`, `applicability_boundary`, `verification_notes`,
            `change_rationale`. `implementation_status` must be one of `draft`, `implemented`,
            `reviewed`, `deprecated`, and defaults to `draft`. `title` and `status` cannot be set
            here. An unknown key, a value that is neither a string nor `null`, or an
            `implementation_status` outside that set is rejected by the input schema before the
            tool runs.

    Returns:
        `{"standard_id", "policy_id", "title", "status"}`; `standard_id` can be used in later calls
        immediately.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The caller is neither the owner nor a `SystemOwner` or `SystemAdmin`.
        - The Policy is not in `draft` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the Standard and its link to the policy graph; no audit event is written.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            graph = _resolve_graph(config, environment=environment)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        access_role_store = environment.access_role_store(config)
        try:
            # Fail closed before any read or write: no write path, no draft write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_add_standard_to_draft(
                actor=actor,
                policy_id=policy_id,
                title=title,
                fields=fields.supplied() if fields is not None else {},
                graph=graph,
                access_role_store=access_role_store,
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _ADD_STANDARD_TO_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "standard_id": result.standard_id,
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
        }

    return _run_mcp_action("add_standard_to_draft", _body, entity_id=policy_id)


_UPDATE_STANDARD_DRAFT_ERRORS = (
    GraphWriteUnavailableError,
    PolicyStandardNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("update-standard-draft", _POLICY_DRAFT_ACCESS_GATE)
def update_standard_draft(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    standard_id: Annotated[str, Field(min_length=1)],
    fields: StandardContentFields | None = None,
) -> dict[str, object] | str:
    """Change a subset of a draft Standard's content fields, leaving the rest as they are.

    A Standard has no owner of its own: only the owner of its parent Policy, or a caller holding
    `SystemOwner` or `SystemAdmin`, may change it. Usable only while the Standard itself (not the
    parent Policy) is in `draft` status. Requires a real authenticated session (the local-test
    bypass
    counts as one).

    Args:
        standard_id: Id of the Standard to change; at least 1 character.
        fields: Partial-update map; only the supplied keys change. Each value is a string or JSON
            `null`, which clears the field. Allowed keys: `description`, `implementation_status`,
            `procedure`, `implementer_role`, `reviewer_role`, `applicability_boundary`,
            `verification_notes`, `change_rationale`. `implementation_status` must be one of
            `draft`,
            `implemented`, `reviewed`, `deprecated`. `title` and `status` can never be patched. An
            unknown key, a value that is neither a string nor `null`, or an `implementation_status`
            outside that set is rejected by the input schema before the tool runs. Omit it or pass
            `null` for a no-op update.

    Returns:
        `{"standard_id", "policy_id", "title", "status"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Standard has `standard_id`.
        - The caller is neither the parent Policy's owner nor a `SystemOwner` or `SystemAdmin`.
        - The Standard is not in `draft` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the changed fields to the policy graph; no audit event is written.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            graph = _resolve_graph(config, environment=environment)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        access_role_store = environment.access_role_store(config)
        try:
            # Fail closed before any read or write: no write path, no draft write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_update_standard_draft(
                actor=actor,
                standard_id=standard_id,
                fields=fields.supplied() if fields is not None else {},
                graph=graph,
                access_role_store=access_role_store,
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _UPDATE_STANDARD_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "standard_id": result.standard_id,
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
        }

    return _run_mcp_action("update_standard_draft", _body, entity_id=standard_id)


_ADD_CONTROL_TO_DRAFT_ERRORS = (
    GraphWriteUnavailableError,
    PolicyStandardNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("add-control-to-draft", _POLICY_DRAFT_ACCESS_GATE)
def add_control_to_draft(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    standard_id: Annotated[str, Field(min_length=1)],
    title: Annotated[str, Field(min_length=1)],
    control_type: Literal["automated", "manual"] = "manual",
    fields: NewControlContentFields | None = None,
) -> dict[str, object] | str:
    """Add a new Control under a draft Standard.

    Ownership comes from the Standard's parent Policy: only that Policy's owner, or a caller holding
    `SystemOwner` or `SystemAdmin`, may add a Control, and only while the Standard itself (not the
    parent Policy) is in `draft` status. Requires a real authenticated session (the local-test
    bypass
    counts as one). The Control is linked to the Standard with `IMPLEMENTED_BY` and always starts in
    `draft` governance status, whatever its `implementation_status`.

    Args:
        standard_id: Id of the draft Standard; at least 1 character.
        title: Control title; at least 1 character.
        control_type: `"automated"` or `"manual"`; default `"manual"`. This is the only way to set
            the type at creation; any other value is rejected by the input schema before the tool
            runs.
        fields: Optional content to set at creation. Each value is a string, or JSON `null`, which
            leaves the field unset. Allowed keys: `description`, `implementation_status`,
            `execution_frequency`, `last_test_date`, `next_review_date`, `evidence_ref`,
            `pass_fail_criteria`, `execution_method`, `evidence_plan`, `executor_role`,
            `reviewer_role`,
            `risk_alignment_rationale`. `implementation_status` must be one of `planned`,
            `implemented`, `reviewed`, `deprecated`, and defaults to `planned`. `title`, `status`
            and
            `type` cannot be set here; use `control_type`. An unknown key (including `type`), a
            value that is neither a string nor `null`, or an `implementation_status` outside that
            set is rejected by the input schema before the tool runs.

    Returns:
        `{"control_id", "standard_id", "policy_id", "title", "status"}`; `control_id` can be used in
        later calls immediately.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Standard has `standard_id`.
        - The caller is neither the parent Policy's owner nor a `SystemOwner` or `SystemAdmin`.
        - The Standard is not in `draft` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the Control and its link to the policy graph; no audit event is written.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            graph = _resolve_graph(config, environment=environment)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        access_role_store = environment.access_role_store(config)
        try:
            # Fail closed before any read or write: no write path, no draft write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_add_control_to_draft(
                actor=actor,
                standard_id=standard_id,
                title=title,
                control_type=control_type,
                fields=fields.supplied() if fields is not None else {},
                graph=graph,
                access_role_store=access_role_store,
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _ADD_CONTROL_TO_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "control_id": result.control_id,
            "standard_id": result.standard_id,
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
        }

    return _run_mcp_action("add_control_to_draft", _body, entity_id=standard_id)


_UPDATE_CONTROL_DRAFT_ERRORS = (
    GraphWriteUnavailableError,
    PolicyControlNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("update-control-draft", _POLICY_DRAFT_ACCESS_GATE)
def update_control_draft(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    control_id: Annotated[str, Field(min_length=1)],
    fields: ControlContentFields | None = None,
) -> dict[str, object] | str:
    """Change a subset of a draft Control's content fields, leaving the rest as they are.

    Ownership comes from the root Policy two levels up (Policy to Standard to Control): only that
    Policy's owner, or a caller holding `SystemOwner` or `SystemAdmin`, may change the Control.
    Usable
    only while the Control itself (not its Standard or Policy) is in `draft` status. Requires a real
    authenticated session (the local-test bypass counts as one).

    Args:
        control_id: Id of the Control to change; at least 1 character.
        fields: Partial-update map; only the supplied keys change. Each value is a string or JSON
            `null`, which clears the field. Allowed keys: `description`, `implementation_status`,
            `type`, `execution_frequency`, `last_test_date`, `next_review_date`, `evidence_ref`,
            `pass_fail_criteria`, `execution_method`, `evidence_plan`, `executor_role`,
            `reviewer_role`,
            `risk_alignment_rationale`. Unlike `add-control-to-draft`, `type` can be changed here,
            and
            this is the only way to change it after creation. `implementation_status` must be one of
            `planned`, `implemented`, `reviewed`, `deprecated`; `type` must be `automated` or
            `manual`. `title` and `status` can never be patched. An unknown key, a value that is
            neither a string nor `null`, or a value outside an enum is rejected by the input schema
            before the tool runs. Omit it or pass `null` for a no-op update.

    Returns:
        `{"control_id", "standard_id", "policy_id", "title", "status"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Control has `control_id`.
        - The caller is neither the root Policy's owner nor a `SystemOwner` or `SystemAdmin`.
        - The Control is not in `draft` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the changed fields to the policy graph; no audit event is written.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            graph = _resolve_graph(config, environment=environment)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        access_role_store = environment.access_role_store(config)
        try:
            # Fail closed before any read or write: no write path, no draft write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_update_control_draft(
                actor=actor,
                control_id=control_id,
                fields=fields.supplied() if fields is not None else {},
                graph=graph,
                access_role_store=access_role_store,
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _UPDATE_CONTROL_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "control_id": result.control_id,
            "standard_id": result.standard_id,
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
        }

    return _run_mcp_action("update_control_draft", _body, entity_id=control_id)


def register_policy_draft_tools(
    server: MCPServer, environment: GateEnvironment, gates: dict[str, ToolGate]
) -> None:
    """Register the policy-draft tools.

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
            create_policy_draft,
            update_policy_draft,
            add_standard_to_draft,
            update_standard_draft,
            add_control_to_draft,
            update_control_draft,
        ),
    )
