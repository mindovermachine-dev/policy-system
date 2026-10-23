"""Near-miss review tools of the PS MCP server: list, resolve and check a near-miss approval.

Registers `near_misses_list`, `near_misses_resolve` and `near_misses_check_approval` on the shared
`server` from `tool_core`. This module imports only `tool_core` (never another tool group); the
patch seam names (`PsycopgPendingApprovalStore`, `PsycopgAuditStore`, `connect_from_config`) are
looked up there at call time as `tool_core.<Name>`.

Business Rules:
    - `keep-separate` needs no role beyond the plain-read gate; the `merge` decision is role-gated
      in the tool body and writes one `access.gate` audit row when denied.
    - A merge decision on a review creates a passkey-signed pending approval bound to the verified
      `(sub, iss)` of the caller; with no verified bearer token it fails closed.
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
    check_near_miss_merge_approval,
    run_list_near_misses,
    run_request_near_miss_merge,
    run_resolve_near_miss,
)
from ps_service.audit.errors import (
    AuditPersistenceError,
    AuditPostgresUnavailableError,
    AuditTrailUnavailableError,
)
from ps_service.config import load_config
from ps_service.graph_cleanup.dependencies import build_default_graph_cleanup_dependencies
from ps_service.graph_cleanup.errors import (
    GraphCleanupAcknowledgmentRequiredError,
    GraphCleanupPersistenceError,
    GraphCleanupValidationError,
)
from ps_service.mcp_interface import tool_core
from ps_service.mcp_interface.errors import McpGraphUnavailableError
from ps_service.mcp_interface.tool_core import (
    CLEANUP_APPROVAL_UNSETTLED_MESSAGE as _CLEANUP_APPROVAL_UNSETTLED_MESSAGE,
)
from ps_service.mcp_interface.tool_core import (
    GRAPH_UNAVAILABLE_MESSAGE as _GRAPH_UNAVAILABLE_MESSAGE,
)
from ps_service.mcp_interface.tool_core import NEAR_MISS_MERGE_GATE as _NEAR_MISS_MERGE_GATE
from ps_service.mcp_interface.tool_core import OWN_APPROVAL_READ_GATE as _OWN_APPROVAL_READ_GATE
from ps_service.mcp_interface.tool_core import PLAIN_READ_GATE as _PLAIN_READ_GATE
from ps_service.mcp_interface.tool_core import audit_context as _audit_context
from ps_service.mcp_interface.tool_core import gate_environment as _gate_environment
from ps_service.mcp_interface.tool_core import gated_server_tool as _gated_tool
from ps_service.mcp_interface.tool_core import resolve_authz_actor as _resolve_authz_actor
from ps_service.mcp_interface.tool_core import resolve_base_url as _resolve_base_url
from ps_service.mcp_interface.tool_core import run_mcp_action as _run_mcp_action
from ps_service.mcp_interface.tool_core import (
    sanitize_cleanup_graph_open as _sanitize_cleanup_graph_open,
)
from ps_service.mcp_interface.tool_core import sanitize_graph_open as _sanitize_graph_open
from ps_service.mcp_interface.tool_gates import run_body_gate

if TYPE_CHECKING:
    from ps_service.api.near_miss_review_orchestration import NearMissReviewDependencies
    from ps_service.config import ServiceConfig


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


@_gated_tool("near_misses_list", _PLAIN_READ_GATE)
def near_misses_list() -> dict[str, object] | str:
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
                dependencies=_sanitize_near_miss_review_graph_opens(
                    build_default_near_miss_review_dependencies()
                ),
            )
        except McpGraphUnavailableError:
            return _GRAPH_UNAVAILABLE_MESSAGE
        except ReviewKindUnsupportedError as exc:
            return f"error: {exc}"
        return result.model_dump()

    return _run_mcp_action("near_misses_list", _body)


def _resolve_merge_pending_approval(
    review_id: str, ctx: Context, config: ServiceConfig, *, acknowledge: bool
) -> dict[str, object] | str:
    """Handle `near_misses_resolve` with `decision="merge"` by requesting a cleanup merge approval.

    Split out of the tool body to keep its branch count manageable; no other tool uses it.

    Args:
        review_id: Id of the near-miss pending review to merge.
        ctx: MCP request context, used to derive the approval link's base URL.
        config: Resolved service configuration.
        acknowledge: True when the caller accepts the governance change of a case-2 merge.

    Returns:
        `{"preview", "pending_approval_id", "approval_url", "expires_at"}`; for a Capability case 2
        without the acknowledgment `{"preview", "acknowledgment_required": True, "message"}` and no
        approval; or an `error: ` string when the caller is not an authenticated Compliance
        Officer, the review does not exist or is stale or is of a kind with no merge, a Role review
        carries the governance acknowledgment, the pair is governed by different Policies, or the
        graph is unreachable.

    Raises:
        Exception: Any other error propagates to `_run_mcp_action`.

    Side Effects:
        Inserts one pending-approval row on success. The merge itself runs only after a human signs.
        Nothing is written to the graph.
    """
    actor = run_body_gate(_gate_environment, "near_misses_resolve")
    if isinstance(actor, str):
        return actor
    try:
        approval = run_request_near_miss_merge(
            review_id,
            actor=actor,
            acknowledge_governance_change=acknowledge,
            base_url=_resolve_base_url(ctx),
            config=config,
            dependencies=_sanitize_near_miss_review_graph_opens(
                build_default_near_miss_review_dependencies()
            ),
            store=tool_core.PsycopgPendingApprovalStore(config),
        )
    except GraphCleanupAcknowledgmentRequiredError as exc:
        return {
            "preview": exc.preview.model_dump(mode="json"),
            "acknowledgment_required": True,
            "message": str(exc),
        }
    except (
        PendingReviewNotFoundError,
        ReviewKindUnsupportedError,
        GraphCleanupValidationError,
    ) as exc:
        return f"error: {exc}"
    except McpGraphUnavailableError, GraphCleanupPersistenceError:
        return _GRAPH_UNAVAILABLE_MESSAGE
    return {
        "preview": approval.preview.model_dump(mode="json"),
        "pending_approval_id": approval.pending_approval_id,
        "approval_url": approval.approval_url,
        "expires_at": approval.expires_at,
    }


# The merge-only fields of `ResolveReviewResponse`; a keep-separate result never carries them.
_MERGE_ONLY_RESOLVE_FIELDS = {"preview", "acknowledgment_required", "message"}


@_gated_tool("near_misses_resolve", _NEAR_MISS_MERGE_GATE)
def near_misses_resolve(
    review_id: Annotated[str, Field(min_length=1)],
    decision: Literal["keep-separate", "merge"],
    ctx: Context,
    *,
    acknowledge_governance_change: bool = False,
) -> dict[str, object] | str:
    """Resolve one near-miss pending review by keeping the two entities separate or merging them.

    `decision="keep-separate"` deletes only the `PendingReview` record and leaves both entities
    untouched. `decision="merge"` merges the two entities through the graph-cleanup approval of the
    review's kind (`merge-capabilities` for a Capability review, `merge-roles` for a Role review)
    and requires an explicit `ComplianceOfficer` grant on a real authenticated session (no admin
    override, never available under the local-test bypass). This call never edits the graph: it
    picks the survivor (a Capability: the one with the earlier `created_at`, a missing one counts
    earliest and a tie goes to the incoming node; a Role carries no `created_at`, so the incoming
    Role survives), returns the merge preview and creates a pending passkey approval bound to that
    pair and state, with the review id signed into it. The merge runs only when the officer opens
    `approval_url` in a browser and signs with a passkey; use `near_misses_check_approval` to
    learn whether that happened.

    On execution the absorbed entity is deleted and a marker (`MergedCapability` or `MergedRole`)
    naming the survivor remains, so a later ingest or restore resolves to the survivor and raises
    no new near miss. A Capability's `REQUIRES`, `COVERS` and `MITIGATED_BY` edges move to the
    survivor; a Role's Obligations move to the surviving Role. The change is one all-or-nothing
    graph write, audited first (`capability.merge` or `role.merge`, linked to the review id and
    reversible with `unmerge`), that also deletes the `PendingReview`. A Capability keeps at most
    one governing Policy: when exactly one of the two is governed the preview names the Policy
    (`policy_case` 2) and the first call, without `acknowledge_governance_change`, returns
    `acknowledgment_required` and creates no approval; repeat it with the flag set once the officer
    has accepted the change. Two Capabilities governed by different Policies are refused before any
    approval, with an error naming `release-capability-governance`. A Role has no governing Policy,
    so `acknowledge_governance_change=true` on a Role review is refused. Needs no LLM Interface.

    Args:
        review_id: Id of the pending review, from `near_misses_list`; at least 1 character.
        decision: `"keep-separate"` or `"merge"`; any other value is rejected before the tool runs.
        ctx: Supplied by the MCP runtime, not by the caller.
        acknowledge_governance_change: `true` to accept the governance change of a case-2
            Capability merge; default `false`. It has no effect on `keep-separate` or on the other
            Capability merge cases, and a Role review refuses `true`.

    Returns:
        For `keep-separate`: `{"review_id", "decision", "winner_id": None, "loser_id": None,
        "pending_approval_id": None, "approval_url": None, "expires_at": None}`. For `merge`:
        `{"preview": {"survivor_id", "survivor_name", "absorbed_id", "absorbed_name", "policy_case",
        "edges_to_move": {"requires", "covers", "mitigated_by"}, "duplicate_edges_collapsed",
        "obligations_affected", "state_digest", "governance"}, "pending_approval_id",
        "approval_url", "expires_at"}` for a Capability review (a Role review's preview is the Role
        merge preview), where `approval_url` is the link a human opens to sign. For a case-2
        Capability merge without the acknowledgment: `{"preview", "acknowledgment_required": true,
        "message"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - `review_id` does not exist, was already resolved, or no longer matches two live
          nodes of its kind (stale).
        - The review has a kind with no registered spec; the error names the kind and nothing is
          audited or changed.
        - (`merge` only) The caller is not an authenticated Compliance Officer.
        - (`merge` only) The review is of a kind that has no merge (the error names it), the two
          Capabilities are governed by different Policies, or `acknowledge_governance_change` was
          set for a Role review.
        - (`keep-separate` only) The audit trail cannot record the resolve; the review is left
          untouched.
        - The policy graph database cannot be reached.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        `keep-separate` writes a `near_miss.resolve` audit row, then deletes the `PendingReview`
        record. `merge` inserts one pending-approval record and writes nothing to the graph; the
        merge is audited when its approval is signed.
        A `merge` caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    actor = _resolve_authz_actor(config)

    def _body() -> dict[str, object] | str:
        if decision == "merge":
            return _resolve_merge_pending_approval(
                review_id, ctx, config, acknowledge=acknowledge_governance_change
            )
        audit = _audit_context(config, actor)
        if isinstance(audit, str):
            return audit
        try:
            result = run_resolve_near_miss(
                review_id,
                decision,
                config=config,
                dependencies=_sanitize_near_miss_review_graph_opens(
                    build_default_near_miss_review_dependencies()
                ),
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
        return result.model_dump(exclude=_MERGE_ONLY_RESOLVE_FIELDS)

    return _run_mcp_action("near_misses_resolve", _body)


@_gated_tool("near_misses_check_approval", _OWN_APPROVAL_READ_GATE)
def near_misses_check_approval(
    pending_approval_id: Annotated[str, Field(min_length=1)],
) -> dict[str, object] | str:
    """Check whether a near-miss merge's pending approval has been signed and what it did.

    `near_misses_resolve` with `decision="merge"` never waits for a human to complete the WebAuthn
    signing ceremony, so this is the separate, poll-again-later way to learn whether it has
    happened. The approval is a graph-cleanup `merge-capabilities` approval that carries the review
    id; any other approval is reported as not found. Ownership check: only the caller who created
    the approval may check it. A caller with no real authenticated session, or whose identity
    differs from the approval's, gets the same generic not-found error as for a wrong id, so a
    caller cannot learn whether an id belongs to someone else.

    Args:
        pending_approval_id: The `pending_approval_id` returned by `near_misses_resolve`; at least 1
            character.

    Returns:
        `{"pending_approval_id", "status", "review_id", "decision", "winner_id", "loser_id",
        "error"}`. `status` is `"pending"`, `"expired"` (the signing window elapsed unsigned;
        derived live, never stored) or `"signed"`. `winner_id` (the surviving Capability) and
        `loser_id` (the absorbed one, deleted behind a `MergedCapability` marker) are populated
        once the signed merge has run, and `error` when the signed approval could not be carried
        out; otherwise they are `None`.

    Errors (returned as a string beginning `error: `, never raised):
        - No such pending approval is visible to this caller.
        - The approval status could not be settled right now (the graph or audit trail is
          unreachable).
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        A signed approval whose outcome was never recorded is settled against the graph once it is
        more than five minutes past its expiry, as `check-cleanup-approval` does. Otherwise the call
        is read-only and logged.
    """
    config = load_config()

    def _body() -> dict[str, object] | str:
        actor = _resolve_signing_actor()
        try:
            status = check_near_miss_merge_approval(
                pending_approval_id=pending_approval_id,
                actor=actor,
                store=tool_core.PsycopgPendingApprovalStore(config),
                config=config,
                dependencies=_sanitize_cleanup_graph_open(
                    build_default_graph_cleanup_dependencies()
                ),
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
            "review_id": status.review_id,
            "decision": status.decision,
            "winner_id": status.winner_id,
            "loser_id": status.loser_id,
            "error": status.error,
        }

    return _run_mcp_action("near_misses_check_approval", _body)
