"""Near-miss review tools of the PS MCP server: list, resolve, and merge many under one approval.

Registers `list-near-misses`, `resolve-near-miss`, `merge-near-misses` and
`check-near-miss-merges`; `register_near_miss_tools` registers them on a server built by
`mcp_server.build_server`. Each tool opens its graph and builds its stores through the
`GateEnvironment` it is bound to. This module imports only `tool_core` and `tool_gates` (never
another tool group).

Business Rules:
    - `resolve-near-miss` only keeps a pair separate and needs no role beyond authentication.
    - `merge-near-misses` needs an explicit `ComplianceOfficer` grant on a real session and creates
      one approval that binds every chosen merge; `check-near-miss-merges` shows only the
      creator's own batch.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Annotated, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import Context
from pydantic import Field

from ps_service.api.errors import PendingReviewNotFoundError, ReviewKindUnsupportedError
from ps_service.api.near_miss_review_orchestration import (
    build_default_near_miss_review_dependencies,
    run_list_near_misses,
    run_resolve_near_miss,
)
from ps_service.audit.errors import (
    AuditPersistenceError,
    AuditPostgresUnavailableError,
    AuditTrailUnavailableError,
)
from ps_service.config import load_config
from ps_service.graph_cleanup.errors import (
    GraphCleanupPersistenceError,
    GraphCleanupValidationError,
)
from ps_service.graph_cleanup.near_miss_batch import create_near_miss_merge_batch
from ps_service.graph_cleanup.near_miss_batch_models import (
    NearMissBatchAcknowledgmentRequiredError,
)
from ps_service.graph_cleanup.near_miss_batch_status import check_near_miss_merge_batch
from ps_service.mcp_interface.errors import McpGraphUnavailableError
from ps_service.mcp_interface.tool_core import (
    CLEANUP_APPROVAL_UNSETTLED_MESSAGE as _CLEANUP_APPROVAL_UNSETTLED_MESSAGE,
)
from ps_service.mcp_interface.tool_core import CLEANUP_GATE as _CLEANUP_GATE
from ps_service.mcp_interface.tool_core import (
    GRAPH_UNAVAILABLE_MESSAGE as _GRAPH_UNAVAILABLE_MESSAGE,
)
from ps_service.mcp_interface.tool_core import (
    NEAR_MISS_KEEP_SEPARATE_GATE as _NEAR_MISS_KEEP_SEPARATE_GATE,
)
from ps_service.mcp_interface.tool_core import OWN_APPROVAL_READ_GATE as _OWN_APPROVAL_READ_GATE
from ps_service.mcp_interface.tool_core import PLAIN_READ_GATE as _PLAIN_READ_GATE
from ps_service.mcp_interface.tool_core import audit_context as _audit_context
from ps_service.mcp_interface.tool_core import cleanup_dependencies as _cleanup_dependencies
from ps_service.mcp_interface.tool_core import declared_tool as _gated_tool
from ps_service.mcp_interface.tool_core import gated_actor as _gated_actor
from ps_service.mcp_interface.tool_core import resolve_authz_actor as _resolve_authz_actor
from ps_service.mcp_interface.tool_core import resolve_base_url as _resolve_base_url
from ps_service.mcp_interface.tool_core import run_mcp_action as _run_mcp_action
from ps_service.mcp_interface.tool_core import (
    sanitize_cleanup_graph_open as _sanitize_cleanup_graph_open,
)
from ps_service.mcp_interface.tool_core import sanitize_graph_open as _sanitize_graph_open
from ps_service.mcp_interface.tool_gates import GateEnvironment, register_declared_tools

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from ps_service.api.near_miss_review_orchestration import NearMissReviewDependencies
    from ps_service.mcp_interface.tool_gates import ToolGate


def _resolve_signing_actor() -> tuple[str, str] | None:
    """Resolve the `(sub, iss)` identity a passkey-signed approval is bound to.

    A signed approval is bound to both subject and issuer, not subject alone, which mirrors the
    `sub`/`iss` extraction `RestAuthMiddleware.__call__` performs on the same `AccessToken` shape.
    There is deliberately no fallback to `LOCAL_TEST_PRINCIPAL_ID`: under the local-test bypass no
    real actor exists that a WebAuthn credential could belong to, so no pending approval may be
    created. Callers treat `None` as a fail-closed condition.

    Returns:
        The verified `(sub, iss)` pair, or `None` if no verified bearer token is bound to this call,
        the token has no subject, or its `iss` claim is not a string.
    """
    access_token = get_access_token()
    if access_token is None or access_token.subject is None:
        return None
    iss = (access_token.claims or {}).get("iss")
    if not isinstance(iss, str):
        return None
    return access_token.subject, iss


def _sanitize_near_miss_review_graph_opens(
    dependencies: NearMissReviewDependencies,
) -> NearMissReviewDependencies:
    """Return `dependencies` with `open_single_tenant_graph` wrapped by `_sanitize_graph_open`.

    The near-miss list and resolve orchestration call that opener directly with no error handling of
    their own.
    """
    return dataclasses.replace(
        dependencies,
        open_single_tenant_graph=_sanitize_graph_open(dependencies.open_single_tenant_graph),
    )


def _review_dependencies(environment: GateEnvironment) -> NearMissReviewDependencies:
    """Return the near-miss review dependencies over the environment's graph factory.

    The graph opener is wrapped so any failure opening the graph becomes `McpGraphUnavailableError`.
    """
    return _sanitize_near_miss_review_graph_opens(
        build_default_near_miss_review_dependencies(
            open_single_tenant_graph=environment.graph_factory
        )
    )


@_gated_tool("list-near-misses", _PLAIN_READ_GATE)
def list_near_misses(  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
) -> dict[str, object] | str:
    """List every unresolved near-miss pending review.

    A near-miss is a Company Merge deduplication candidate awaiting a keep-separate or merge
    decision. Reads the merged compliance graph. Takes no parameters and needs no LLM Interface,
    because deduplication runs only during ingestion and merge, never during this read.

    Returns:
        A dict with a `reviews` list (empty when none are unresolved), one entry per unresolved
        review with `id`, `kind`, `incoming_text`, `nearest_existing_text` and `similarity`
        (0.0-1.0). The two canonical node ids a review references are deliberately omitted.

    Errors (returned as a string beginning `error: `, never raised):
        - The policy graph database cannot be reached.
        - A stored review has a kind with no registered spec; the error names the kind and no
          review is listed.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        None on the graph. The call is logged.
    """
    config = load_config()

    def _body() -> dict[str, object] | str:
        try:
            result = run_list_near_misses(
                config=config,
                dependencies=_review_dependencies(environment),
            )
        except McpGraphUnavailableError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        except ReviewKindUnsupportedError as exc:
            return f"error: {exc}"
        return result.model_dump()

    return _run_mcp_action("list-near-misses", _body)


@_gated_tool("resolve-near-miss", _NEAR_MISS_KEEP_SEPARATE_GATE)
def resolve_near_miss(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    review_id: Annotated[str, Field(min_length=1)],
    decision: Literal["keep-separate"],
) -> dict[str, object] | str:
    """Resolve one near-miss pending review by keeping the two entities separate.

    Deletes only the `PendingReview` record and leaves both entities untouched. Any authenticated
    caller may use it. Merging near misses is `merge-near-misses`. An argument this tool does not
    declare is rejected before it runs. Needs no LLM Interface.

    Args:
        review_id: Id of the pending review, from `list-near-misses`; at least 1 character.
        decision: Only `"keep-separate"`; any other value is rejected before the tool runs.

    Returns:
        `{"review_id", "decision"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - `review_id` does not exist, was already resolved, or no longer matches two live
          nodes of its kind (stale).
        - The review has a kind with no registered spec; the error names the kind and nothing is
          audited or changed.
        - The audit trail cannot record the resolve; the review is left untouched.
        - The policy graph database cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes a `near_miss.resolve` audit row, then deletes the `PendingReview` record.
    """
    config = load_config()
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        audit = _audit_context(config, actor, environment=environment)
        if isinstance(audit, str):
            return audit
        try:
            result = run_resolve_near_miss(
                review_id,
                decision,
                config=config,
                dependencies=_review_dependencies(environment),
                audit=audit,
            )
        except (
            PendingReviewNotFoundError,
            ReviewKindUnsupportedError,
            AuditTrailUnavailableError,
        ) as exc:
            return f"error: {exc}"
        except McpGraphUnavailableError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return {"review_id": result.review_id, "decision": result.decision}

    return _run_mcp_action("resolve-near-miss", _body)


@_gated_tool("merge-near-misses", _CLEANUP_GATE)
def merge_near_misses(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    review_ids: list[str],
    ctx: Context,
    acknowledged_review_ids: list[str] | None = None,
) -> dict[str, object] | str:
    """Preview merging a hand-picked set of near-miss reviews and request one passkey approval.

    Requires an explicit `ComplianceOfficer` grant on a real authenticated session (no admin
    override; never available under the local-test bypass). This call never edits the graph: it
    returns one preview entry per review and creates one pending passkey approval that binds every
    merge (review id, survivor, absorbed node, state digest, acknowledgment). The merges run only
    when the officer opens `approval_url` in a browser and signs with a passkey; use
    `check-near-miss-merges` to learn what happened to each one.

    Capability and Role reviews may be mixed. The survivor of each pair is picked by the same rule
    as a single merge: a Capability with the earlier `created_at` (a missing one counts earliest
    and a tie goes to the incoming node); a Role carries no `created_at`, so the incoming Role
    survives. There is no limit on the number of reviews.

    Args:
        review_ids: Ids of the reviews to merge, from `list-near-misses`, in the order chosen; at
            least one, no duplicates.
        ctx: Supplied by the server, not the client; used to build the base of `approval_url`.
        acknowledged_review_ids: Ids of the selected reviews whose governance change the officer
            accepts; default none.

    Returns:
        `{"merges": [{"review_id", "kind", "acknowledged", "survivor_id", "survivor_name",
        "absorbed_id", "absorbed_name", "state_digest", ...}], "pending_approval_id",
        "approval_url", "expires_at"}`, one `merges` entry per review in the order given. A
        Capability entry also has `policy_case`, `edges_to_move`, `duplicate_edges_collapsed`,
        `obligations_affected` and `governance`; a Role entry has `instrument_id` and
        `moved_obligation_ids`. When a Capability merge changes what a Policy governs and its review
        id is not in `acknowledged_review_ids`, no approval is created and the result is instead
        `{"acknowledgment_required": true, "reviews": [{"review_id", "policy": {"id", "title",
        "status"}}], "message", "merges": [...]}`; repeat the call with those review ids in
        `acknowledged_review_ids`.

    Errors (returned as a string beginning `error: `, never raised; no approval is created on any
    error):
        - The caller is not an authenticated Compliance Officer.
        - Two selected reviews share a node, a Capability pair is governed by two different
          Policies (use `release-capability-governance`) or has a non-active side; the error names
          each such review.
        - An acknowledged review id is not selected, is a Role review or changes no governance.
        - A selected review does not exist, is already resolved or stale, or is of a kind with no
          merge; the error names every such review with its state.
        - The policy graph database cannot be reached.
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
            approval = create_near_miss_merge_batch(
                graph,
                review_ids=review_ids,
                acknowledged_review_ids=acknowledged_review_ids or [],
                actor=actor,
                base_url=_resolve_base_url(ctx),
                store=environment.pending_approval_store_factory(config),
            )
        except NearMissBatchAcknowledgmentRequiredError as exc:
            return {
                "acknowledgment_required": True,
                "reviews": [needed.wire_form() for needed in exc.reviews],
                "message": str(exc),
                "merges": [entry.wire_form() for entry in exc.merges],
            }
        except GraphCleanupValidationError as exc:
            return f"error: {exc}"
        except McpGraphUnavailableError, GraphCleanupPersistenceError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        return {
            "merges": [entry.wire_form() for entry in approval.merges],
            "pending_approval_id": approval.pending_approval_id,
            "approval_url": approval.approval_url,
            "expires_at": approval.expires_at,
        }

    return _run_mcp_action("merge-near-misses", _body)


@_gated_tool("check-near-miss-merges", _OWN_APPROVAL_READ_GATE)
def check_near_miss_merges(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment,
    pending_approval_id: Annotated[str, Field(min_length=1)],
) -> dict[str, object] | str:
    """Check whether a near-miss merge batch has been signed and what happened to each merge.

    `merge-near-misses` never waits for a human to complete the WebAuthn signing ceremony, so this
    is the separate, poll-again-later way to learn whether it has happened. Ownership check: only
    the caller who created the approval may check it. A caller with no real authenticated session,
    whose identity differs from the approval's, or who passes an id that is not a batch approval
    gets the same generic not-found error as for a wrong id.

    Args:
        pending_approval_id: The `pending_approval_id` returned by `merge-near-misses`; at least 1
            character.

    Returns:
        `{"pending_approval_id", "status", "merges"}`. `status` is `"pending"`, `"expired"` (the
        signing window elapsed unsigned; derived live, never stored) or `"signed"`. `merges` is
        empty until signed, then has one entry per review id with `review_id`, `kind`, `state`
        (`merged`, `failed` or `unsettled`), `winner_id` and `loser_id` (set once merged) and
        `error` (set when that merge was not carried out). `unsettled` means the outcome of that
        merge is not yet known (the signing was interrupted and has not been settled yet); check
        again later, and do not treat it as merged or as failed.

    Errors (returned as a string beginning `error: `, never raised):
        - No such pending approval is visible to this caller.
        - The status of a signed batch could not be settled right now (graph or audit trail
          unreachable); try again shortly.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Logged. When a signed batch's outcome was never stored and it is more than five minutes
        past its expiry, each merge is settled against the graph and the audit trail: one
        `failed` audit row per merge that did not happen, and the settled outcome is stored. No
        merge is ever run again by this call.
    """
    config = load_config()

    def _body() -> dict[str, object] | str:
        try:
            status = check_near_miss_merge_batch(
                pending_approval_id=pending_approval_id,
                actor=_resolve_signing_actor(),
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
            "merges": [merge.model_dump() for merge in status.merges],
        }

    return _run_mcp_action("check-near-miss-merges", _body)


def register_near_miss_tools(
    server: MCPServer, environment: GateEnvironment, gates: dict[str, ToolGate]
) -> None:
    """Register the near-miss review tools.

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
            list_near_misses,
            resolve_near_miss,
            merge_near_misses,
            check_near_miss_merges,
        ),
    )
