"""Graph-cleanup tools of the PS MCP server.

Registers `find-capability-merge-candidates`, `find-duplicate-obligations`, `merge-capabilities`,
`merge-obligations`, `merge-roles`, `release-capability-governance`, `unmerge` and
`check-cleanup-approval`; `register_graph_cleanup_tools` registers them on a server built by
`mcp_server.build_server`. Each tool delegates to `ps_service.graph_cleanup.service`, which owns the
behaviour, and builds its graph opener and stores through the `GateEnvironment` it is bound to. This
module imports only `tool_core` and `tool_gates` (never another tool group).

Business Rules:
    - Every cleanup tool needs an explicit `ComplianceOfficer` grant on a real authenticated
      session; there is no admin override and no local-test bypass.
    - A merge, release or unmerge creates a pending approval and changes nothing in the graph until
      the human approval is settled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from mcp.server.mcpserver import Context
from pydantic import Field

from ps_service.audit.errors import AuditPersistenceError, AuditPostgresUnavailableError
from ps_service.config import load_config
from ps_service.graph_cleanup.discovery import resolve_min_similarity
from ps_service.graph_cleanup.errors import (
    GraphCleanupAcknowledgmentRequiredError,
    GraphCleanupPersistenceError,
    GraphCleanupValidationError,
)
from ps_service.graph_cleanup.service import (
    check_cleanup_approval,
    create_capability_merge_approval,
    create_obligation_merge_approval,
    create_release_governance_approval,
    create_role_merge_approval,
    create_unmerge_approval,
    find_capability_merge_candidates,
    find_duplicate_obligations,
)
from ps_service.mcp_interface.errors import McpGraphUnavailableError
from ps_service.mcp_interface.tool_core import (
    CLEANUP_APPROVAL_UNSETTLED_MESSAGE as _CLEANUP_APPROVAL_UNSETTLED_MESSAGE,
)
from ps_service.mcp_interface.tool_core import CLEANUP_GATE as _CLEANUP_GATE
from ps_service.mcp_interface.tool_core import (
    GRAPH_UNAVAILABLE_MESSAGE as _GRAPH_UNAVAILABLE_MESSAGE,
)
from ps_service.mcp_interface.tool_core import cleanup_dependencies as _cleanup_dependencies
from ps_service.mcp_interface.tool_core import declared_tool as _gated_tool
from ps_service.mcp_interface.tool_core import gated_actor as _gated_actor
from ps_service.mcp_interface.tool_core import resolve_base_url as _resolve_base_url
from ps_service.mcp_interface.tool_core import run_mcp_action as _run_mcp_action
from ps_service.mcp_interface.tool_core import (
    sanitize_cleanup_graph_open as _sanitize_cleanup_graph_open,
)
from ps_service.mcp_interface.tool_core import sanitize_graph_open as _sanitize_graph_open
from ps_service.mcp_interface.tool_gates import GateEnvironment, register_declared_tools

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from ps_service.mcp_interface.tool_gates import ToolGate


@_gated_tool("find-capability-merge-candidates", _CLEANUP_GATE)
def find_capability_merge_candidates_tool(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    min_similarity: Annotated[float | None, Field(gt=0.5, le=1.0)] = None,
) -> dict[str, object] | str:
    """List groups of active Capabilities that look like duplicates.

    This is a read-only graph cleanup aid.

    Nothing is changed and no approval is created. Requires an explicit `ComplianceOfficer` grant on
    a
    real authenticated session (no admin override; never available under the local-test bypass).
    Capabilities are linked when the cosine similarity of their already-cached embeddings reaches
    the
    threshold; no embedding is fetched or computed by this tool.

    Args:
        min_similarity: Cosine similarity cut-off, greater than 0.5 and at most 1.0. When omitted,
            the configured Capability merge threshold applies, else 0.90.

    Returns:
        `{"groups": [{"basis", "merge_case", "policies_distinct", "members": [{"id", "name",
        "obligation_count", "governing_policy"}, ...]}, ...]}`. `basis` is `"name"` when every
        member's
        name is equal once case and punctuation are ignored, else `"embedding"`. `governing_policy`
        is
        `{"id", "title", "status"}` or `null`; `obligation_count` is the number of Obligations
        requiring
        that Capability. `merge_case` is 1 (no member governed), 2 (exactly one governed) or 3 (two
        or
        more governed); `policies_distinct` is true when the governed members name different
        Policies.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session or lacks the `ComplianceOfficer` role.
        - The authorization store or the policy graph cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the policy graph and the authorization store; nothing is written. The call is logged.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()

    def _body() -> dict[str, object] | str:
        dependencies = _cleanup_dependencies(environment)
        try:
            graph = _sanitize_graph_open(dependencies.open_single_tenant_graph)(config)
            result = find_capability_merge_candidates(
                graph,
                min_similarity=resolve_min_similarity(
                    min_similarity, config.company_merge_capability_merge_threshold
                ),
            )
        except McpGraphUnavailableError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        except GraphCleanupPersistenceError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return result.model_dump()

    return _run_mcp_action("find_capability_merge_candidates", _body)


@_gated_tool("find-duplicate-obligations", _CLEANUP_GATE)
def find_duplicate_obligations_tool(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment, role_id: str | None = None
) -> dict[str, object] | str:
    """List groups of duplicate Obligations under one Role, as a read-only graph cleanup aid.

    Nothing is changed and no approval is created. Requires an explicit `ComplianceOfficer` grant on
    a
    real authenticated session (no admin override; never available under the local-test bypass).
    Only
    Obligations under the same Role are grouped, and only when their text is identical or
    near-identical
    once case and punctuation are ignored; the same wording under two Roles is never grouped.

    Args:
        role_id: Optional Role id; when given, the sweep covers only that Role.

    Returns:
        `{"groups": [{"role_id", "role_name", "basis", "members": [{"id", "text", "requirements":
        [{"requirement_id", "source_ref"}, ...]}, ...]}, ...]}`. `basis` is `"identical_text"` or
        `"near_text"`. Each `source_ref` comes from the `EXPRESSES` edge of a Requirement the
        Obligation
        satisfies.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session or lacks the `ComplianceOfficer` role.
        - The authorization store or the policy graph cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the policy graph and the authorization store; nothing is written. The call is logged.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()

    def _body() -> dict[str, object] | str:
        dependencies = _cleanup_dependencies(environment)
        try:
            graph = _sanitize_graph_open(dependencies.open_single_tenant_graph)(config)
            result = find_duplicate_obligations(graph, role_id=role_id)
        except McpGraphUnavailableError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        except GraphCleanupPersistenceError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return result.model_dump()

    return _run_mcp_action("find_duplicate_obligations", _body)


@_gated_tool("merge-capabilities", _CLEANUP_GATE)
def merge_capabilities_tool(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    survivor_id: Annotated[str, Field(min_length=1)],
    absorbed_id: Annotated[str, Field(min_length=1)],
    ctx: Context,
    *,
    acknowledge_governance_change: bool = False,
) -> dict[str, object] | str:
    """Preview merging one Capability into another and request a passkey approval for the merge.

    Requires an explicit `ComplianceOfficer` grant on a real authenticated session (no admin
    override;
    never available under the local-test bypass). This call never edits the graph: it returns a
    preview
    and creates a pending passkey approval bound to this exact pair and to the state previewed. The
    merge runs only when the officer opens `approval_url` in a browser and signs with a passkey;
    `check-cleanup-approval` reports the outcome.

    On execution the absorbed Capability's `REQUIRES`, `COVERS` and `MITIGATED_BY` edges move to the
    survivor with no duplicate edges, and the absorbed node is deleted, leaving a
    `MergedCapability` marker that names the survivor. The change is one all-or-nothing graph write
    that is audited first (`capability.merge`).

    Merge case 1 (neither Capability has a governing Policy) needs nothing more. Case 2 (exactly one
    is
    governed) changes which Capability that Policy governs: the first call, without
    `acknowledge_governance_change`, returns the preview with `acknowledgment_required: true` and a
    `message`, and creates no approval; repeat the call with `acknowledge_governance_change=true`
    once
    the officer has explicitly accepted that change, and the acknowledgment becomes part of the
    signed
    approval and the audit row (`policy_case` 2). The preview's `governance` block names the Policy
    (`id`, `title`, `status`), `obligations_coverage_changed`, and the Policy's governed set before
    and
    after; an `approved` Policy keeps its content and version. Case 3 (both governed by the same
    Policy)
    needs no acknowledgment: the absorbed Capability's `GOVERNED_BY` edge is deleted and the
    survivor's
    stays (`policy_case` 3). Two Capabilities governed by different Policies are rejected before any
    approval, with an error naming both Policies and the `release-capability-governance` step; when
    neither Policy is a draft that step is unavailable and the error says there is no completion
    path
    (a Policy fork carries the whole governed set).

    Args:
        survivor_id: Id of the Capability that remains; at least 1 character.
        absorbed_id: Id of the Capability merged into the survivor; at least 1 character.
        acknowledge_governance_change: Set to `true` to accept the governance change of merge case
            2;
            default `false`. It has no effect on the other cases.
        ctx: Supplied by the server, not the client; used to build the base of `approval_url`.

    Returns:
        `{"preview": {"survivor_id", "survivor_name", "absorbed_id", "absorbed_name", "policy_case",
        "edges_to_move": {"requires", "covers", "mitigated_by"}, "duplicate_edges_collapsed",
        "obligations_affected", "state_digest", "governance"}, "pending_approval_id",
        "approval_url",
        "expires_at"}` (`governance` is `null` in case 1). For case 2 without the acknowledgment it
        returns `{"preview", "acknowledgment_required": true, "message"}` instead.

    Errors (returned as a string beginning `error: `, never raised; no approval is created on any
    error):
        - The caller is not an authenticated Compliance Officer.
        - A side is the same node as the other, does not exist, or is not active.
        - The two Capabilities are governed by different Policies.
        - The policy graph database or the approval store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the policy graph and creates one pending approval row with an expiry and writes one
        log entry; the graph is not changed.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()

    def _body() -> dict[str, object] | str:
        actor = _gated_actor(config)
        dependencies = _cleanup_dependencies(environment)
        try:
            graph = _sanitize_graph_open(dependencies.open_single_tenant_graph)(config)
            approval = create_capability_merge_approval(
                graph,
                survivor_id=survivor_id,
                absorbed_id=absorbed_id,
                acknowledge_governance_change=acknowledge_governance_change,
                actor=actor,
                base_url=_resolve_base_url(ctx),
                store=environment.pending_approval_store_factory(config),
            )
        except GraphCleanupAcknowledgmentRequiredError as exc:
            return {
                "preview": exc.preview.model_dump(mode="json"),
                "acknowledgment_required": True,
                "message": str(exc),
            }
        except GraphCleanupValidationError as exc:
            return f"error: {exc}"
        except McpGraphUnavailableError, GraphCleanupPersistenceError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return {
            "preview": approval.preview.model_dump(mode="json"),
            "pending_approval_id": approval.pending_approval_id,
            "approval_url": approval.approval_url,
            "expires_at": approval.expires_at,
        }

    return _run_mcp_action("merge_capabilities", _body)


@_gated_tool("merge-obligations", _CLEANUP_GATE)
def merge_obligations_tool(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    survivor_id: Annotated[str, Field(min_length=1)],
    absorbed_id: Annotated[str, Field(min_length=1)],
    ctx: Context,
) -> dict[str, object] | str:
    """Preview merging one Obligation into another and request a passkey approval for the merge.

    Requires an explicit `ComplianceOfficer` grant on a real authenticated session (no admin
    override;
    never available under the local-test bypass). This call never edits the graph: it returns a
    preview
    and creates a pending passkey approval bound to this exact pair and to the state previewed. The
    merge runs only when the officer opens `approval_url` in a browser and signs with a passkey;
    `check-cleanup-approval` reports the outcome.

    Both Obligations must be borne by the same Role. On execution the absorbed Obligation's
    `SATISFIED_BY` and `REQUIRES` edges are unioned onto the survivor (no duplicate edges), the
    survivor
    keeps its single `HAS` Role edge, and the absorbed Obligation is deleted, with its full node and
    edge snapshot held in the audit row (`obligation.merge`). The change is one all-or-nothing graph
    write that is audited first. The deletion leaves a `MergedObligation` marker so that a later
    ingest
    or restore that regenerates the absorbed Obligation attaches to the survivor instead of
    recreating
    it.

    Args:
        survivor_id: Id of the Obligation that remains; at least 1 character.
        absorbed_id: Id of the Obligation merged into the survivor; at least 1 character.
        ctx: Supplied by the server, not the client; used to build the base of `approval_url`.

    Returns:
        `{"preview": {"survivor_id", "survivor_text", "absorbed_id", "absorbed_text", "role_id",
        "role_name", "edges_to_move": {"satisfied_by", "requires"}, "duplicate_edges_collapsed",
        "requirement_source_refs": [{"requirement_id", "source_ref"}], "state_digest"},
        "pending_approval_id", "approval_url", "expires_at"}`.

    Errors (returned as a string beginning `error: `, never raised; no approval is created on any
    error):
        - The caller is not an authenticated Compliance Officer.
        - A side is the same node as the other or does not exist.
        - The two Obligations are under different Roles.
        - The policy graph database or the approval store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the policy graph and creates one pending approval row with an expiry and writes one
        log entry; the graph is not changed.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()

    def _body() -> dict[str, object] | str:
        actor = _gated_actor(config)
        dependencies = _cleanup_dependencies(environment)
        try:
            graph = _sanitize_graph_open(dependencies.open_single_tenant_graph)(config)
            approval = create_obligation_merge_approval(
                graph,
                survivor_id=survivor_id,
                absorbed_id=absorbed_id,
                actor=actor,
                base_url=_resolve_base_url(ctx),
                store=environment.pending_approval_store_factory(config),
            )
        except GraphCleanupValidationError as exc:
            return f"error: {exc}"
        except McpGraphUnavailableError, GraphCleanupPersistenceError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return {
            "preview": approval.preview.model_dump(mode="json"),
            "pending_approval_id": approval.pending_approval_id,
            "approval_url": approval.approval_url,
            "expires_at": approval.expires_at,
        }

    return _run_mcp_action("merge_obligations", _body)


@_gated_tool("merge-roles", _CLEANUP_GATE)
def merge_roles_tool(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    survivor_id: Annotated[str, Field(min_length=1)],
    absorbed_id: Annotated[str, Field(min_length=1)],
    ctx: Context,
    review_id: Annotated[str | None, Field(min_length=1)] = None,
) -> dict[str, object] | str:
    """Preview merging one Role into another and request a passkey approval for the merge.

    Requires an explicit `ComplianceOfficer` grant on a real authenticated session (no admin
    override; never available under the local-test bypass). This call never edits the graph: it
    returns a preview and creates a pending passkey approval bound to this exact pair and to the
    state previewed. The merge runs only when the officer opens `approval_url` in a browser and
    signs with a passkey; `check-cleanup-approval` reports the outcome.

    Both Roles must be defined by the same RegulatoryInstrument (Roles stay instrument-scoped).
    On execution every Obligation the absorbed Role bears moves to the survivor and keeps its id,
    the absorbed Role's `DEFINES` edge is removed, and the absorbed Role is deleted, with its full
    node and edge snapshot held in the audit row (`role.merge`). The survivor's own `DEFINES`
    edge is unchanged. The change is one all-or-nothing graph write that is audited first. The
    deletion leaves a `MergedRole` marker so that a later ingest or restore that regenerates the
    absorbed Role attaches to the survivor instead of recreating it.

    When the merge resolves a Role near-miss review, pass its id as `review_id`: the review must be
    a Role review that names exactly this pair (either way round). It is signed with the pair, is
    re-checked when the approval is signed, and is deleted in the same write as the merge.

    Args:
        survivor_id: Id of the Role that remains; at least 1 character.
        absorbed_id: Id of the Role merged into the survivor; at least 1 character.
        ctx: Supplied by the server, not the client; used to build the base of `approval_url`.
        review_id: Id of the Role `PendingReview` this merge resolves; at least 1 character when
            given. Omit it for a plain cleanup merge.

    Returns:
        `{"preview": {"survivor_id", "survivor_name", "absorbed_id", "absorbed_name",
        "instrument_id", "moved_obligation_ids", "state_digest"}, "pending_approval_id",
        "approval_url", "expires_at"}`.

    Errors (returned as a string beginning `error: `, never raised; no approval is created on any
    error):
        - The caller is not an authenticated Compliance Officer.
        - The pair cannot be merged: the same id twice, an id that is not a Role or is a merged
          Role's marker, or two Roles defined by different instruments.
        - `review_id` names no review, a review of another kind, or a review of another pair.
        - The policy graph database or the approval store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the policy graph and creates one pending approval row with an expiry and writes one
        log entry; the graph is not changed.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()

    def _body() -> dict[str, object] | str:
        actor = _gated_actor(config)
        dependencies = _cleanup_dependencies(environment)
        try:
            graph = _sanitize_graph_open(dependencies.open_single_tenant_graph)(config)
            approval = create_role_merge_approval(
                graph,
                survivor_id=survivor_id,
                absorbed_id=absorbed_id,
                actor=actor,
                base_url=_resolve_base_url(ctx),
                store=environment.pending_approval_store_factory(config),
                review_id=review_id,
            )
        except GraphCleanupValidationError as exc:
            return f"error: {exc}"
        except McpGraphUnavailableError, GraphCleanupPersistenceError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return {
            "preview": approval.preview.model_dump(mode="json"),
            "pending_approval_id": approval.pending_approval_id,
            "approval_url": approval.approval_url,
            "expires_at": approval.expires_at,
        }

    return _run_mcp_action("merge_roles", _body)


@_gated_tool("release-capability-governance", _CLEANUP_GATE)
def release_capability_governance_tool(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    capability_id: Annotated[str, Field(min_length=1)],
    ctx: Context,
) -> dict[str, object] | str:
    """Preview releasing a Capability from its draft Policy and request a passkey approval.

    Requires an explicit `ComplianceOfficer` grant on a real authenticated session (no admin
    override;
    never available under the local-test bypass). This call never edits the graph: it returns a
    preview
    and creates a pending passkey approval bound to this Capability, its governing Policy and the
    state
    previewed. The release runs only when the officer opens `approval_url` in a browser and signs
    with a
    passkey; `check-cleanup-approval` reports the outcome.

    Only a Capability governed by a `draft` Policy can be released. On execution its single
    `GOVERNED_BY`
    edge to that Policy is deleted: the Capability and the Policy stay and the Capability becomes
    ungoverned. The change is one all-or-nothing graph write that is audited first
    (`capability.release_governance`, with a before and after snapshot). A `proposed` Policy is
    rejected
    with a pointer to `revert-policy-to-draft`; an `approved` or `deprecated` Policy is rejected
    with a
    pointer to the Policy lifecycle, stating that a fork carries the whole governed set and so does
    not
    by itself free the Capability.

    Args:
        capability_id: Id of the Capability to release; at least 1 character.
        ctx: Supplied by the server, not the client; used to build the base of `approval_url`.

    Returns:
        `{"preview": {"capability_id", "capability_name", "policy_id", "policy_title",
        "policy_status",
        "governed_set_before", "governed_set_after", "state_digest"}, "pending_approval_id",
        "approval_url", "expires_at"}`.

    Errors (returned as a string beginning `error: `, never raised; no approval is created on any
    error):
        - The caller is not an authenticated Compliance Officer.
        - The Capability does not exist, is not active, or is not governed.
        - Its governing Policy is not a draft.
        - The policy graph database or the approval store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the policy graph and creates one pending approval row with an expiry and writes one
        log entry; the graph is not changed.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()

    def _body() -> dict[str, object] | str:
        actor = _gated_actor(config)
        dependencies = _cleanup_dependencies(environment)
        try:
            graph = _sanitize_graph_open(dependencies.open_single_tenant_graph)(config)
            approval = create_release_governance_approval(
                graph,
                capability_id=capability_id,
                actor=actor,
                base_url=_resolve_base_url(ctx),
                store=environment.pending_approval_store_factory(config),
            )
        except GraphCleanupValidationError as exc:
            return f"error: {exc}"
        except McpGraphUnavailableError, GraphCleanupPersistenceError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return {
            "preview": approval.preview.model_dump(mode="json"),
            "pending_approval_id": approval.pending_approval_id,
            "approval_url": approval.approval_url,
            "expires_at": approval.expires_at,
        }

    return _run_mcp_action("release_capability_governance", _body)


_AUDIT_UNREADABLE_MESSAGE = "error: the audit trail could not be read right now; try again shortly"


@_gated_tool("unmerge", _CLEANUP_GATE)
def unmerge_tool(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    merged_id: Annotated[str, Field(min_length=1)],
    ctx: Context,
) -> dict[str, object] | str:
    """Preview reversing a Compliance Officer merge and request a passkey approval for the reversal.

    Requires an explicit `ComplianceOfficer` grant on a real authenticated session (no admin
    override;
    never available under the local-test bypass). The merge is found in the audit trail
    (`capability.merge`, `obligation.merge` or `role.merge`): the newest `applied` row not followed
    by a `failed` row
    for the same approval. This call never edits the graph: it returns a preview and creates a
    pending
    passkey approval bound to the merge it reverses and to the state previewed. The unmerge runs
    only
    when the officer opens `approval_url` in a browser and signs with a passkey;
    `check-cleanup-approval`
    reports the outcome.

    Capability merge: the deleted Capability is recreated under its original id with its properties
    and its `MergedCapability` marker is removed, and exactly
    the edges the merge moved off it are restored from the audit snapshot (the survivor keeps any
    edge it
    already had before the merge). Edges added to the survivor since the merge stay in place and are
    listed as `survivor_added_edges`.

    Obligation merge: the deleted Obligation is recreated under its original id with its properties,
    its
    `HAS` edge from the Role and exactly its `SATISFIED_BY` and `REQUIRES` edges from the audit
    snapshot,
    and its `MergedObligation` marker is removed. The survivor's edges are never removed, because a
    union
    cannot be attributed: edges added since the merge are listed as `survivor_added_edges`, and
    those
    that may have come from the absorbed Obligation as `survivor_edges_possibly_from_merge`.

    Role merge (`merge-roles`): the deleted Role is recreated under its original id with its
    original properties and its `DEFINES` edge from the instrument (with its `source_ref`),
    exactly the Obligations that merge moved are borne by it again (each leaves the survivor), and
    its `MergedRole` marker is removed. Obligations the survivor gained since the merge stay on the
    survivor and are counted in `survivor_obligations_added_since_merge`. A Role merge that a later
    merge depends on cannot be reversed: a later `obligation.merge` of a moved Obligation, or a
    later `role.merge` of the survivor, is refused with the audit event id of that merge; reverse
    that one first.

    Either way the change is one all-or-nothing graph write that is audited first
    (`capability.unmerge`, `obligation.unmerge` or `role.unmerge`, with a before and after
    snapshot).

    Args:
        merged_id: Id absorbed by `merge-capabilities` (a deleted Capability), by
            `merge-obligations` (a deleted Obligation) or by `merge-roles` (a deleted Role); at
            least 1 character.
        ctx: Supplied by the server, not the client; used to build the base of `approval_url`.

    Returns:
        `{"preview": {"kind": "capability" | "obligation" | "role", "merged_id", "survivor_id",
        "merge_approval_id", "state_digest", ...}, "pending_approval_id", "approval_url",
        "expires_at"}`. A capability or obligation preview also carries `edges_to_restore` and
        `survivor_added_edges`. A capability preview also carries `merged_name`, `survivor_name`
        and `edges_removed_from_survivor`; an obligation preview carries `merged_text`,
        `survivor_text`, `role_id`, `survivor_edges_possibly_from_merge` and `note`; a role
        preview carries `merged_name`, `survivor_name`, `instrument_id`, `obligations_to_restore`,
        `defines_source_ref`, `survivor_obligations_added_since_merge` and `note`.

    Errors (returned as a string beginning `error: `, never raised; no approval is created on any
    error):
        - The caller is not an authenticated Compliance Officer.
        - No merge of the id is in the audit trail.
        - The merge cannot be reversed because of a conflict (the survivor was merged away or
          re-pointed,
          its governance changed, a restored edge's endpoint is gone, the Obligation or Role exists
          again, or a moved Obligation is gone or borne by another Role);
          the message explains which.
        - A later merge depends on the Role merge; the message names that merge's audit event id.
        - The audit trail, the policy graph database or the approval store cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the audit trail and the policy graph and creates one pending approval row with an
        expiry;
        the graph is not changed.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()

    def _body() -> dict[str, object] | str:
        actor = _gated_actor(config)
        dependencies = _cleanup_dependencies(environment)
        try:
            graph = _sanitize_graph_open(dependencies.open_single_tenant_graph)(config)
            approval = create_unmerge_approval(
                graph,
                dependencies.audit_store(config),
                merged_id=merged_id,
                actor=actor,
                base_url=_resolve_base_url(ctx),
                store=environment.pending_approval_store_factory(config),
            )
        except GraphCleanupValidationError as exc:
            return f"error: {exc}"
        except McpGraphUnavailableError, GraphCleanupPersistenceError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        except AuditPostgresUnavailableError, AuditPersistenceError:
            return _AUDIT_UNREADABLE_MESSAGE
        return {
            "preview": approval.preview.model_dump(mode="json"),
            "pending_approval_id": approval.pending_approval_id,
            "approval_url": approval.approval_url,
            "expires_at": approval.expires_at,
        }

    return _run_mcp_action("unmerge", _body)


@_gated_tool("check-cleanup-approval", _CLEANUP_GATE)
def check_cleanup_approval_tool(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    pending_approval_id: Annotated[str, Field(min_length=1)],
) -> dict[str, object] | str:
    """Report the status and outcome of a graph-cleanup passkey approval.

    Companion to `merge-capabilities`, `merge-obligations`, `merge-roles`,
    `release-capability-governance` and `unmerge`: the passkey ceremony happens in a browser, so
    this is how to learn whether the approval was signed and what the edit did. Requires an
    explicit `ComplianceOfficer` grant. Only the officer who created the approval can see it; any
    other caller, or an unknown id, gets the same not-found error.

    Args:
        pending_approval_id: The `pending_approval_id` returned by the tool that created the
            approval;
            at least 1 character.

    Returns:
        `{"pending_approval_id", "status", "tool_name", "outcome"}`. `status` is `pending`,
        `expired`
        (the window elapsed unsigned, derived live) or `signed`. `outcome` is `null` until the
        signed
        approval has run, then one of: the result (`{"survivor_id", "absorbed_id", "merged": true}`
        for
        any merge tool, `{"capability_id", "policy_id", "released": true}` for a release,
        `{"merged_id", "survivor_id", "unmerged": true, "survivor_added_edges"}` for an unmerge of
        a capability or obligation merge,
        `{"merged_id", "survivor_id", "unmerged": true, "survivor_obligations_added_since_merge"}`
        for an unmerge of a role merge), `{"error": <message>}`, or `{"reconciled": "applied"}`. An
        `applied` audit row followed by
        a `failed` row for the same approval id means no edit occurred.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller is not an authenticated Compliance Officer.
        - No such approval is visible to the caller.
        - The approval status could not be settled right now (the graph or audit trail is
          unreachable).
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        A signed approval whose outcome was never recorded is checked against the graph once it is
        more
        than five minutes past its expiry, and settled: if the edit is present the outcome becomes
        `reconciled`; if not, a `failed` audit row is written and the outcome becomes an error.
        Otherwise nothing is written.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()

    def _body() -> dict[str, object] | str:
        actor = _gated_actor(config)
        try:
            status = check_cleanup_approval(
                pending_approval_id=pending_approval_id,
                actor=actor,
                store=environment.pending_approval_store_factory(config),
                config=config,
                dependencies=_sanitize_cleanup_graph_open(_cleanup_dependencies(environment)),
            )
        except (
            McpGraphUnavailableError,
            GraphCleanupPersistenceError,
            AuditPostgresUnavailableError,
            AuditPersistenceError,
        ):
            return _CLEANUP_APPROVAL_UNSETTLED_MESSAGE
        if status is None:
            return f"error: no pending approval with id {pending_approval_id!r}"
        return {
            "pending_approval_id": status.pending_approval_id,
            "status": status.status,
            "tool_name": status.tool_name,
            "outcome": status.outcome,
        }

    return _run_mcp_action("check_cleanup_approval", _body)


def register_graph_cleanup_tools(
    server: MCPServer, environment: GateEnvironment, gates: dict[str, ToolGate]
) -> None:
    """Register the graph-cleanup tools.

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
            find_capability_merge_candidates_tool,
            find_duplicate_obligations_tool,
            merge_capabilities_tool,
            merge_obligations_tool,
            merge_roles_tool,
            release_capability_governance_tool,
            unmerge_tool,
            check_cleanup_approval_tool,
        ),
    )
