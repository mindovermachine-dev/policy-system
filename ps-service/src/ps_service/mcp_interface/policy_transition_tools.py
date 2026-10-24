"""Policy-transition tools of the PS MCP server.

Declares `get-policy`, `propose-policy`, `approve-policy`, `reject-policy` and
`revert-policy-to-draft`; `register_policy_transition_tools` registers them on a server built by
`mcp_server.build_server`. Each tool delegates to `ps_service.policy_lifecycle.service`, which owns
the behaviour, and builds its graph and stores through the `GateEnvironment` it is bound to. This
module imports only `tool_core` and `tool_gates` (never another tool group).

Business Rules:
    - `approve-policy` and `reject-policy` are body-gated, so an unknown policy id is reported
      before an access denial.
    - Domain failures are returned as `error: <message>` strings, never raised across the MCP
      boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from ps_service.api.errors import (
    AccessDeniedError,
    AuthorizationStoreUnavailableError,
    GraphWriteUnavailableError,
)
from ps_service.api.graph_write_access import graph_write_access
from ps_service.config import load_config
from ps_service.mcp_interface.errors import McpGraphUnavailableError
from ps_service.mcp_interface.tool_core import POLICY_AUTHOR_GATE as _POLICY_AUTHOR_GATE
from ps_service.mcp_interface.tool_core import POLICY_DRAFT_ACCESS_GATE as _POLICY_DRAFT_ACCESS_GATE
from ps_service.mcp_interface.tool_core import (
    POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE,
)
from ps_service.mcp_interface.tool_core import POLICY_TRANSITION_GATE as _POLICY_TRANSITION_GATE
from ps_service.mcp_interface.tool_core import declared_tool as _gated_tool
from ps_service.mcp_interface.tool_core import resolve_graph as _resolve_graph
from ps_service.mcp_interface.tool_core import (
    resolve_policy_lifecycle_actor as _resolve_policy_lifecycle_actor,
)
from ps_service.mcp_interface.tool_core import run_mcp_action as _run_mcp_action
from ps_service.mcp_interface.tool_gates import (
    GateEnvironment,
    ToolGate,
    lifecycle_role_enforcer,
    register_declared_tools,
)
from ps_service.policy_lifecycle.errors import (
    PolicyDraftAccessDeniedError,
    PolicyGovernanceConflictError,
    PolicyIncompleteForProposalError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
    PolicyNotFoundError,
    PolicySelfApprovalBlockedError,
)
from ps_service.policy_lifecycle.graph_submission import lifecycle_submitter
from ps_service.policy_lifecycle.service import approve_policy as run_approve_policy
from ps_service.policy_lifecycle.service import get_policy as run_get_policy
from ps_service.policy_lifecycle.service import propose_policy as run_propose_policy
from ps_service.policy_lifecycle.service import reject_policy as run_reject_policy
from ps_service.policy_lifecycle.service import revert_policy_to_draft as run_revert_policy_to_draft

if TYPE_CHECKING:
    from mcp.server import MCPServer


_GET_POLICY_ERRORS = (
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("get-policy", _POLICY_DRAFT_ACCESS_GATE)
def get_policy(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    policy_id: Annotated[str, Field(min_length=1)],
) -> dict[str, object] | str:
    """Read one Policy together with its full Standard and Control tree.

    A Policy is visible to any authenticated caller in every status, `draft` included, so a draft
    can appear beside approved content; check `status` before treating a Policy as in force.
    Requires a real authenticated session (the local-test bypass counts as one).

    Args:
        policy_id: Id of the Policy to read; at least 1 character.

    Returns:
        `{"policy_id", "title", "status", "version", "owner_subject", "owner_issuer", "standards"}`,
        where `standards` is a list of `{"standard_id", "title", "status", "controls"}` and each
        `controls` is a list of `{"control_id", "title", "control_type", "status"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the policy graph. When an older Policy tree is missing data this read can backfill it,
        which writes to the graph; a tree that needs no backfill is not written.
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
        try:
            result = run_get_policy(
                policy_id=policy_id,
                graph=graph,
                # Resolved only if a backfill must be written (a read of a backfilled tree needs
                # no gateway).
                submitter_source=lambda: lifecycle_submitter(graph_write_access(config).gateway),
                graph_name=config.falkordb_graph,
            )
        except _GET_POLICY_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
            "version": result.version,
            "owner_subject": result.owner_subject,
            "owner_issuer": result.owner_issuer,
            "standards": [
                {
                    "standard_id": standard.standard_id,
                    "title": standard.title,
                    "status": standard.status,
                    "controls": [
                        {
                            "control_id": control.control_id,
                            "title": control.title,
                            "control_type": control.control_type,
                            "status": control.status,
                        }
                        for control in standard.controls
                    ],
                }
                for standard in result.standards
            ],
        }

    return _run_mcp_action("get_policy", _body)


_PROPOSE_POLICY_ERRORS = (
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyIncompleteForProposalError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("propose-policy", _POLICY_AUTHOR_GATE)
def propose_policy(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    policy_id: Annotated[str, Field(min_length=1)],
) -> dict[str, object] | str:
    """Move a draft Policy and its whole tree to `proposed`.

    Only the Policy's owner may propose it, and only while it is in `draft` status with at least one
    Standard attached. Requires a real authenticated session (the local-test bypass counts as one).

    Args:
        policy_id: Id of the Policy to propose; at least 1 character.

    Returns:
        `{"policy_id", "status", "standard_ids", "control_ids"}`, listing every Standard and Control
        in
        the tree.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The caller does not own the Policy.
        - The Policy is not in `draft` status, or has no Standards.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Changes the status of the Policy and every Standard and Control in its tree in one cascading
        graph write and records an audit event.
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
            result = run_propose_policy(
                actor=actor,
                policy_id=policy_id,
                graph=graph,
                audit_store=audit_store,
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _PROPOSE_POLICY_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "status": result.status,
            "standard_ids": list(result.standard_ids),
            "control_ids": list(result.control_ids),
        }

    return _run_mcp_action("propose_policy", _body)


_APPROVE_POLICY_ERRORS = (
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    AccessDeniedError,
    PolicySelfApprovalBlockedError,
    PolicyInvalidStatusTransitionError,
    PolicyGovernanceConflictError,
    PolicyLifecycleGraphUnavailableError,
    AuthorizationStoreUnavailableError,
)


@_gated_tool("approve-policy", _POLICY_TRANSITION_GATE)
def approve_policy(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    policy_id: Annotated[str, Field(min_length=1)],
) -> dict[str, object] | str:
    """Move a proposed Policy and its whole tree to `approved`.

    Requires the `PolicyManager` role, and the caller must not be the Policy's owner: self-approval
    is
    always refused, even for a `PolicyManager` who owns it. Usable only while the Policy is in
    `proposed` status. Requires a real authenticated session (the local-test bypass counts as one).
    When the Policy amends an approved prior Policy (created with `supersedes_policy_id`), that
    prior
    tree is deprecated in the same call, recorded as its own audit event. If the prior Policy
    governs
    Capabilities, their governing edges move to the approved Policy in the same all-or-nothing write
    as
    the status change.

    Args:
        policy_id: Id of the Policy to approve; at least 1 character.

    Returns:
        `{"policy_id", "status", "standard_ids", "control_ids", "auto_deprecated_policy_id",
        "governed_capability_ids"}`. `auto_deprecated_policy_id` is `None` unless a prior Policy was
        deprecated; `governed_capability_ids` lists the Capabilities whose governance moved (empty
        when
        none).

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The caller does not hold `PolicyManager`, or is the Policy's owner.
        - The Policy is not in `proposed` status.
        - The Capabilities governed by the superseded Policy changed during approval; nothing was
          changed, retry.
        - The policy graph or the authorization store cannot be reached, or the write path is
          unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Changes the status of the whole tree (and of the superseded tree, when there is one) and
        moves
        Capability governance in one graph write, and records audit events.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
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
            result = run_approve_policy(
                actor=actor,
                policy_id=policy_id,
                graph=graph,
                audit_store=audit_store,
                enforce_policy_manager=lifecycle_role_enforcer(
                    environment, "approve-policy", _POLICY_TRANSITION_GATE
                ),
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _APPROVE_POLICY_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "status": result.status,
            "standard_ids": list(result.standard_ids),
            "control_ids": list(result.control_ids),
            "auto_deprecated_policy_id": result.auto_deprecated_policy_id,
            "governed_capability_ids": list(result.governed_capability_ids),
        }

    return _run_mcp_action("approve_policy", _body)


_REJECT_POLICY_ERRORS = (
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    AccessDeniedError,
    PolicySelfApprovalBlockedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
    AuthorizationStoreUnavailableError,
)


@_gated_tool("reject-policy", _POLICY_TRANSITION_GATE)
def reject_policy(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    policy_id: Annotated[str, Field(min_length=1)],
) -> dict[str, object] | str:
    """Move a proposed Policy and its whole tree back to `draft`.

    Requires the `PolicyManager` role, and the caller must not be the Policy's owner: self-rejection
    is
    always refused, even for a `PolicyManager` who owns it. Usable only while the Policy is in
    `proposed` status. Unlike approval, no prior Policy is deprecated. Requires a real authenticated
    session (the local-test bypass counts as one).

    Args:
        policy_id: Id of the Policy to reject; at least 1 character.

    Returns:
        `{"policy_id", "status", "standard_ids", "control_ids"}`, listing every Standard and Control
        in
        the tree.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The caller does not hold `PolicyManager`, or is the Policy's owner.
        - The Policy is not in `proposed` status.
        - The policy graph or the authorization store cannot be reached, or the write path is
          unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Changes the status of the Policy and every Standard and Control in its tree in one cascading
        graph write and records an audit event.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
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
            result = run_reject_policy(
                actor=actor,
                policy_id=policy_id,
                graph=graph,
                audit_store=audit_store,
                enforce_policy_manager=lifecycle_role_enforcer(
                    environment, "reject-policy", _POLICY_TRANSITION_GATE
                ),
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _REJECT_POLICY_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "status": result.status,
            "standard_ids": list(result.standard_ids),
            "control_ids": list(result.control_ids),
        }

    return _run_mcp_action("reject_policy", _body)


_REVERT_POLICY_TO_DRAFT_ERRORS = (
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("revert-policy-to-draft", _POLICY_AUTHOR_GATE)
def revert_policy_to_draft(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    policy_id: Annotated[str, Field(min_length=1)],
) -> dict[str, object] | str:
    """Move a proposed Policy and its whole tree back to `draft`, as its owner.

    Owner-only: unlike `reject-policy` there is no `PolicyManager` role check, and a `PolicyManager`
    who is not the owner is refused like any other non-owner. Usable only while the Policy is in
    `proposed` status. Requires a real authenticated session (the local-test bypass counts as one).

    Args:
        policy_id: Id of the Policy to revert; at least 1 character.

    Returns:
        `{"policy_id", "status", "standard_ids", "control_ids"}`, listing every Standard and Control
        in
        the tree.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The caller does not own the Policy.
        - The Policy is not in `proposed` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Changes the status of the Policy and every Standard and Control in its tree in one cascading
        graph write and records an audit event.
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
            result = run_revert_policy_to_draft(
                actor=actor,
                policy_id=policy_id,
                graph=graph,
                audit_store=audit_store,
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _REVERT_POLICY_TO_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "status": result.status,
            "standard_ids": list(result.standard_ids),
            "control_ids": list(result.control_ids),
        }

    return _run_mcp_action("revert_policy_to_draft", _body)


def register_policy_transition_tools(
    server: MCPServer, environment: GateEnvironment, gates: dict[str, ToolGate]
) -> None:
    """Register the policy-transition tools.

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
            get_policy,
            propose_policy,
            approve_policy,
            reject_policy,
            revert_policy_to_draft,
        ),
    )
