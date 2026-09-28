"""`ps_service.policy_lifecycle` service functions (issue #134, PLAN.md S11).

`create_policy_draft` is the first (and, for this slice, only) service
function: the human-authored `create-policy-draft` MCP tool's own delegate
(L2 MCP Interface Patterns' "delegate, don't reimplement" rule -- the tool
itself, S12, is a thin wrapper over this function).

Importing `ps_service.policy_lifecycle.audit_actions` here (for its
import-time `register_audit_action` side effect) is deliberate and required:
per that module's own docstring, "the component that actually emits
`policy.*` audit events... is responsible for importing this module so
registration happens before any event is recorded" -- this is that
component.

Ordering (D-9, applied to the single-step `create_policy_draft` case since
there is no prior status to transition from): (1) compute the v1
`policy_id(title)`; (2) a pure read (`graph_writer.find_existing_policy`)
checks for a title collision (AC-BI-022) -- on collision, record a
`outcome="rejected"` audit event (plan decision, for audit completeness)
then raise, no graph write ever attempted; (3) on no collision, record the
`outcome="applied"` audit event BEFORE the graph write (AC-BI-016); (4)
mint the Policy (+ optional Standard/Control children, always
`status="draft"`, D-6) via `graph_writer.create_policy_draft`; (5) on a
graph failure, record a follow-up `outcome="failed"` event for the same
action/resource_id, then raise `PolicyLifecycleGraphUnavailableError`
(AC-BI-024) -- the original `redis.exceptions.RedisError` is chained, never
leaked to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import redis.exceptions

import ps_service.policy_lifecycle.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers this component's own audit actions (issue #134) before any create/propose/approve/... call can reach AuditStore.record
from ps_service.authz.models import AccessRole
from ps_service.authz.service import require_role, resolve_active_roles
from ps_service.domain_mapper.identity import control_id, standard_id
from ps_service.domain_mapper.identity import policy_id as compute_policy_id
from ps_service.policy_lifecycle import graph_writer
from ps_service.policy_lifecycle.errors import (
    PolicyDraftAccessDeniedError,
    PolicyIncompleteForProposalError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
    PolicyNotFoundError,
    PolicySelfApprovalBlockedError,
    PolicyTitleAlreadyExistsError,
)
from ps_service.policy_lifecycle.rules import (
    PolicyLifecycleRuleContext,
    block_self_approval,
    require_owner,
    require_status,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Literal

    from ps_service.audit.store import AuditStore
    from ps_service.authz.store import AccessRoleStore
    from ps_service.policy_lifecycle.graph_writer import GraphHandle

    PolicyStatus = Literal["draft", "proposed", "approved", "deprecated"]
    _TransitionReasonCode = Literal[
        "access_denied", "self_approval_blocked", "invalid_status", "incomplete_for_proposal"
    ]

__all__ = [
    "ControlDraftInput",
    "ControlView",
    "PolicyApproveResult",
    "PolicyDraftResult",
    "PolicyProposeResult",
    "PolicyRejectResult",
    "PolicyRevertResult",
    "PolicyView",
    "StandardDraftInput",
    "StandardView",
    "approve_policy",
    "create_policy_draft",
    "get_policy",
    "propose_policy",
    "reject_policy",
    "revert_policy_to_draft",
]

# `AccessRole`s that see a Draft Policy regardless of ownership (issue #134, S13).
_DRAFT_VISIBILITY_OVERRIDE_ROLES = frozenset({AccessRole.SYSTEM_OWNER, AccessRole.SYSTEM_ADMIN})

_CREATE_DRAFT_ACTION = "policy.create_draft"
_PROPOSE_ACTION = "policy.propose"
_APPROVE_ACTION = "policy.approve"
_REJECT_ACTION = "policy.reject"
_REVERT_ACTION = "policy.revert"
_AUTO_DEPRECATE_ACTION = "policy.auto_deprecate"
_POLICY_RESOURCE_TYPE = "policy"


@dataclass(frozen=True, slots=True)
class ControlDraftInput:
    """One Control child a `create_policy_draft` caller wants minted alongside the Policy."""

    title: str
    control_type: Literal["automated", "manual"] = "manual"


@dataclass(frozen=True, slots=True)
class StandardDraftInput:
    """One Standard child (with its own optional Control children) to mint."""

    title: str
    controls: tuple[ControlDraftInput, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PolicyDraftResult:
    """The newly-minted draft Policy's identity and initial state."""

    policy_id: str
    title: str
    status: Literal["draft"]
    version: Literal["1"]
    owner_subject: str
    owner_issuer: str
    standard_ids: tuple[str, ...]
    control_ids: tuple[str, ...]


def _build_standard_drafts(
    new_policy_id: str, standards: tuple[StandardDraftInput, ...]
) -> tuple[graph_writer.StandardDraft, ...]:
    """Compute every Standard/Control child's identity up front (pure, no graph I/O).

    Ids are computed before either audit write so both the `applied` audit
    event's `affected_node_ids` and the eventual graph write use the exact
    same ids.
    """
    drafts: list[graph_writer.StandardDraft] = []
    for standard in standards:
        new_standard_id = standard_id(new_policy_id, standard.title)
        controls = tuple(
            graph_writer.ControlDraft(
                id=control_id(new_standard_id, control.title),
                title=control.title,
                control_type=control.control_type,
            )
            for control in standard.controls
        )
        drafts.append(
            graph_writer.StandardDraft(id=new_standard_id, title=standard.title, controls=controls)
        )
    return tuple(drafts)


def create_policy_draft(
    *,
    actor: tuple[str, str],
    title: str,
    standards: tuple[StandardDraftInput, ...] = (),
    graph: GraphHandle,
    audit_store: AuditStore,
) -> PolicyDraftResult:
    """Create a new draft Policy owned by `actor` (AC-BI-001/009/015/016/022/024).

    Args:
        actor: The creating caller's verified `(sub, iss)` identity --
            becomes the new Policy's `owner_subject`/`owner_issuer`.
        title: The new Policy's title (feeds `policy_id(title)`, v1).
        standards: Optional Standard children (each with its own optional
            Control children) to mint alongside the Policy, all
            unconditionally `status="draft"` (D-6).
        graph: The single-tenant policy graph handle.
        audit_store: Where every `policy.create_draft` audit event is
            recorded, via `record_standalone` -- this call has no
            surrounding state-changing transaction to join.

    Returns:
        A `PolicyDraftResult` describing the newly-minted Policy.

    Raises:
        PolicyTitleAlreadyExistsError: `title`'s v1 id already exists
            (AC-BI-022) -- a rejected audit event is recorded first; no
            graph write is ever attempted.
        PolicyLifecycleGraphUnavailableError: the graph write failed after
            the `applied` audit event was already recorded -- a `failed`
            follow-up event is recorded for the same action/resource_id
            before this is raised (AC-BI-024).
    """
    owner_subject, owner_issuer = actor
    new_policy_id = compute_policy_id(title)

    existing = graph_writer.find_existing_policy(graph, new_policy_id)
    if existing is not None:
        existing_id, _existing_title = existing
        audit_store.record_standalone(
            actor_subject=owner_subject,
            actor_issuer=owner_issuer,
            action=_CREATE_DRAFT_ACTION,
            resource_type=_POLICY_RESOURCE_TYPE,
            resource_id=new_policy_id,
            outcome="rejected",
            details={
                "affected_node_ids": (),
                "to_status": "draft",
                "reason_code": "title_already_exists",
            },
        )
        raise PolicyTitleAlreadyExistsError(title, existing_id)

    standard_drafts = _build_standard_drafts(new_policy_id, standards)
    new_standard_ids = tuple(standard.id for standard in standard_drafts)
    new_control_ids = tuple(
        control.id for standard in standard_drafts for control in standard.controls
    )
    affected_node_ids = (new_policy_id, *new_standard_ids, *new_control_ids)

    audit_store.record_standalone(
        actor_subject=owner_subject,
        actor_issuer=owner_issuer,
        action=_CREATE_DRAFT_ACTION,
        resource_type=_POLICY_RESOURCE_TYPE,
        resource_id=new_policy_id,
        outcome="applied",
        details={"affected_node_ids": affected_node_ids, "to_status": "draft"},
    )

    try:
        graph_writer.create_policy_draft(
            graph,
            policy_id=new_policy_id,
            title=title,
            owner_subject=owner_subject,
            owner_issuer=owner_issuer,
            standards=standard_drafts,
        )
    except redis.exceptions.RedisError as exc:
        audit_store.record_standalone(
            actor_subject=owner_subject,
            actor_issuer=owner_issuer,
            action=_CREATE_DRAFT_ACTION,
            resource_type=_POLICY_RESOURCE_TYPE,
            resource_id=new_policy_id,
            outcome="failed",
            details={"affected_node_ids": affected_node_ids, "to_status": "draft"},
        )
        raise PolicyLifecycleGraphUnavailableError from exc

    return PolicyDraftResult(
        policy_id=new_policy_id,
        title=title,
        status="draft",
        version="1",
        owner_subject=owner_subject,
        owner_issuer=owner_issuer,
        standard_ids=new_standard_ids,
        control_ids=new_control_ids,
    )


@dataclass(frozen=True, slots=True)
class ControlView:
    """One Control in a `get_policy` result's tree."""

    control_id: str
    title: str
    control_type: str
    status: str


@dataclass(frozen=True, slots=True)
class StandardView:
    """One Standard (with its own Control children) in a `get_policy` result's tree."""

    standard_id: str
    title: str
    status: str
    controls: tuple[ControlView, ...]


@dataclass(frozen=True, slots=True)
class PolicyView:
    """A Policy's own fields plus its full Standard/Control tree (issue #134, S13)."""

    policy_id: str
    title: str
    status: PolicyStatus
    version: str
    owner_subject: str
    owner_issuer: str
    standards: tuple[StandardView, ...]


def _to_policy_view(record: graph_writer.PolicyRecord) -> PolicyView:
    """Map `graph_writer.read_policy_tree`'s raw `PolicyRecord` onto this module's own view shape.

    A deliberate, separate shape from `graph_writer.PolicyRecord` (L1 "prefer
    duplication over the wrong abstraction"): the graph-writer record is this
    component's own internal FalkorDB row-grouping detail, while `PolicyView`
    is `get_policy`'s public return contract -- coupling them would mean a
    future change to either one's shape silently changes the other.
    """
    return PolicyView(
        policy_id=record.id,
        title=record.title,
        status=cast("PolicyStatus", record.status),
        version=record.version,
        owner_subject=record.owner_subject,
        owner_issuer=record.owner_issuer,
        standards=tuple(
            StandardView(
                standard_id=standard.id,
                title=standard.title,
                status=standard.status,
                controls=tuple(
                    ControlView(
                        control_id=control.id,
                        title=control.title,
                        control_type=control.control_type,
                        status=control.status,
                    )
                    for control in standard.controls
                ),
            )
            for standard in record.standards
        ),
    )


def get_policy(
    *,
    actor: tuple[str, str],
    policy_id: str,
    graph: GraphHandle,
    access_role_store: AccessRoleStore,
) -> PolicyView:
    """Read `policy_id` plus its full Standard/Control tree (AC-BI-002/AC-BI-020).

    Runs `graph_writer.backfill_governance_status` first (D-7), so a Policy
    minted before this issue's `version`/Standard-or-Control `status`
    properties existed is self-healed before this read (and before the
    visibility gate below ever inspects `status`).

    Visibility gate -- **Draft status only**: the issue's own AC-BI-002 text
    ("attempts to view or act on a Draft Policy") scopes this gate to Draft
    Policies; a Proposed/Approved/Deprecated Policy is readable by any
    already-authenticated caller, no further check. For a Draft Policy,
    `actor` must either be the Policy's own owner
    (`ps_service.policy_lifecycle.rules.require_owner`) or hold `SystemOwner`
    or `SystemAdmin` (`ps_service.authz.service.resolve_active_roles`, reused
    unchanged -- no new authz infrastructure). A non-owner `PolicyManager` is
    **not** carved out by AC-BI-002's literal wording, so it is rejected the
    same as any other non-owner, non-`SystemOwner`/`SystemAdmin` caller.

    Args:
        actor: The calling caller's verified `(sub, iss)` identity.
        policy_id: The Policy id to read.
        graph: The single-tenant policy graph handle.
        access_role_store: Where `actor`'s active `AccessRole`s are resolved
            from, only ever consulted when `policy_id`'s Policy is a Draft
            and `actor` is not its owner.

    Returns:
        A `PolicyView` describing the Policy and its full Standard/Control
        tree.

    Raises:
        PolicyNotFoundError: no `Policy` node exists with `policy_id`.
        PolicyDraftAccessDeniedError: `policy_id`'s Policy is a Draft and
            `actor` is neither its owner nor a `SystemOwner`/`SystemAdmin`.
            No audit event is recorded for this denial -- AC-BI-016 only
            mandates an audit trail for the six write transitions, not reads.
    """
    graph_writer.backfill_governance_status(graph, policy_id)
    record = graph_writer.read_policy_tree(graph, policy_id)
    if record is None:
        raise PolicyNotFoundError(policy_id)

    if record.status == "draft":
        owner = (record.owner_subject, record.owner_issuer)
        context = PolicyLifecycleRuleContext(
            actor=actor,
            owner=owner,
            action="read",
            current_status=cast("PolicyStatus", record.status),
        )
        is_owner = require_owner(context).allowed
        if not is_owner:
            active_roles = resolve_active_roles(actor, store=access_role_store)
            if not (active_roles & _DRAFT_VISIBILITY_OVERRIDE_ROLES):
                raise PolicyDraftAccessDeniedError

    return _to_policy_view(record)


@dataclass(frozen=True, slots=True)
class PolicyProposeResult:
    """A successful `propose_policy` call's resulting state (issue #134, S15)."""

    policy_id: str
    status: Literal["proposed"]
    standard_ids: tuple[str, ...]
    control_ids: tuple[str, ...]


def propose_policy(
    *,
    actor: tuple[str, str],
    policy_id: str,
    graph: GraphHandle,
    audit_store: AuditStore,
) -> PolicyProposeResult:
    """Propose `policy_id`, cascading its whole tree to `status="proposed"` (S15).

    Ordering (D-9): (0) `backfill_governance_status`; (1) a pure read
    (`graph_writer.read_policy_tree`) of the Policy's owner/status and every
    Standard/Control id in its tree; (2) `require_owner` (AC-BI-002/003 --
    only the owner may propose); (3) `require_status` (AC-BI-023 -- must be
    `"draft"`); (4) completeness (AC-BI-013 -- at least one Standard; a
    Standard with zero Controls is never checked, AC-BI-014); (5) on any gate
    or completeness failure, a `outcome="rejected"` audit event (naming the
    failed check's own `reason_code`) is recorded before the named error is
    raised; (6) on all checks passing, the `outcome="applied"` audit event is
    recorded BEFORE the graph write (AC-BI-016); (7)
    `graph_writer.cascade_status` sets the Policy and its entire tree to
    `"proposed"` in one Cypher statement; (8) on a graph failure, a
    `outcome="failed"` follow-up event is recorded for the same
    action/resource_id, then `PolicyLifecycleGraphUnavailableError` is raised
    (AC-BI-024), the original `redis.exceptions.RedisError` chained.

    Args:
        actor: The calling caller's verified `(sub, iss)` identity.
        policy_id: The Policy id to propose.
        graph: The single-tenant policy graph handle.
        audit_store: Where every `policy.propose` audit event is recorded,
            via `record_standalone` -- this call has no surrounding
            state-changing transaction to join.

    Returns:
        A `PolicyProposeResult` describing the now-`"proposed"` Policy and
        its full Standard/Control id tree.

    Raises:
        PolicyNotFoundError: no `Policy` node exists with `policy_id`.
        PolicyDraftAccessDeniedError: `actor` is not `policy_id`'s owner.
        PolicyInvalidStatusTransitionError: `policy_id`'s Policy is not
            currently `"draft"`.
        PolicyIncompleteForProposalError: `policy_id`'s Policy has zero
            Standards.
        PolicyLifecycleGraphUnavailableError: the graph write failed after
            the `applied` audit event was already recorded -- a `failed`
            follow-up event is recorded for the same action/resource_id
            before this is raised (AC-BI-024).
    """
    record = _read_transition_target(graph, policy_id)

    owner = (record.owner_subject, record.owner_issuer)
    current_status = cast("PolicyStatus", record.status)
    standard_ids = tuple(standard.id for standard in record.standards)
    control_ids = tuple(
        control.id for standard in record.standards for control in standard.controls
    )
    affected_node_ids = (policy_id, *standard_ids, *control_ids)
    context = PolicyLifecycleRuleContext(
        actor=actor, owner=owner, action="propose", current_status=current_status
    )

    gates = (
        _TransitionGate(
            allowed=require_owner(context).allowed,
            reason_code="access_denied",
            error=PolicyDraftAccessDeniedError(),
        ),
        _TransitionGate(
            allowed=require_status(context).allowed,
            reason_code="invalid_status",
            error=PolicyInvalidStatusTransitionError(
                action="propose", current_status=current_status, required_status="draft"
            ),
        ),
        _TransitionGate(
            allowed=bool(standard_ids),
            reason_code="incomplete_for_proposal",
            error=PolicyIncompleteForProposalError(),
        ),
    )
    _apply_transition(
        actor=actor,
        policy_id=policy_id,
        spec=_TransitionSpec(
            action=_PROPOSE_ACTION, from_status=current_status, target_status="proposed"
        ),
        affected_node_ids=affected_node_ids,
        gates=gates,
        graph=graph,
        audit_store=audit_store,
    )

    return PolicyProposeResult(
        policy_id=policy_id,
        status="proposed",
        standard_ids=standard_ids,
        control_ids=control_ids,
    )


def _tree_node_ids(record: graph_writer.PolicyRecord) -> tuple[str, ...]:
    """`(policy_id, *standard_ids, *control_ids)` for one already-read tree (S17/S25).

    A small shared helper across `approve_policy`'s two audit-then-cascade
    calls (the successor's own tree, and -- when applicable -- the
    auto-deprecated prior's tree) -- not the cross-*action* D-9 helper
    Slice 26 defers (that one spans `create_policy_draft`/`propose_policy`/
    `approve_policy`/`reject_policy`/`revert_policy_to_draft`'s differing gate
    stacks); this is only ever called from within `approve_policy` itself, on
    two structurally identical `PolicyRecord`s.
    """
    standard_ids = tuple(standard.id for standard in record.standards)
    control_ids = tuple(
        control.id for standard in record.standards for control in standard.controls
    )
    return (record.id, *standard_ids, *control_ids)


def _cascade_with_audit(
    *,
    actor: tuple[str, str],
    action: str,
    policy_id: str,
    affected_node_ids: tuple[str, ...],
    from_status: str,
    target_status: str,
    graph: GraphHandle,
    audit_store: AuditStore,
) -> None:
    """D-9's audit-then-cascade tail: `applied` audit, cascade, `failed` audit on failure.

    Shared by `approve_policy`'s two calls (the successor's own transition to
    `"approved"`, and the auto-deprecated prior's own separate transition to
    `"deprecated"`) -- both are the exact same shape with zero branching
    differences, so factoring out this literal duplication within one
    function is not the premature cross-action abstraction D-9/Slice 26
    explicitly defers (that refactor unifies the *gate* stacks across five
    differently-gated top-level actions; this helper has no gates of its
    own at all -- every gate has already run by the time either caller
    reaches this point).

    Raises:
        PolicyLifecycleGraphUnavailableError: the graph write failed after
            the `applied` audit event was already recorded -- a `failed`
            follow-up event is recorded for the same action/resource_id
            before this is raised (AC-BI-024), the original
            `redis.exceptions.RedisError` chained.
    """
    actor_subject, actor_issuer = actor
    audit_store.record_standalone(
        actor_subject=actor_subject,
        actor_issuer=actor_issuer,
        action=action,
        resource_type=_POLICY_RESOURCE_TYPE,
        resource_id=policy_id,
        outcome="applied",
        details={
            "affected_node_ids": affected_node_ids,
            "from_status": from_status,
            "to_status": target_status,
        },
    )
    try:
        graph_writer.cascade_status(graph, policy_id=policy_id, target_status=target_status)
    except redis.exceptions.RedisError as exc:
        audit_store.record_standalone(
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            action=action,
            resource_type=_POLICY_RESOURCE_TYPE,
            resource_id=policy_id,
            outcome="failed",
            details={
                "affected_node_ids": affected_node_ids,
                "from_status": from_status,
                "to_status": target_status,
            },
        )
        raise PolicyLifecycleGraphUnavailableError from exc


def _read_transition_target(graph: GraphHandle, policy_id: str) -> graph_writer.PolicyRecord:
    """The (backfill, read, not-found) preamble shared by all 4 transition functions (S26).

    Before this refactor, `propose_policy`/`approve_policy`/`reject_policy`/
    `revert_policy_to_draft` each ran the identical
    `graph_writer.backfill_governance_status` + `graph_writer.read_policy_tree`
    + "raise `PolicyNotFoundError` on a miss" sequence, with zero per-action
    variation. `get_policy` (S13) runs the same two calls but is
    deliberately NOT routed through this helper -- it is a read tool, not a
    transition, and PLAN.md's own S26 scope names only the 4 transition
    functions (`create_policy_draft` likewise stays untouched: it has no
    prior tree to read at all).
    """
    graph_writer.backfill_governance_status(graph, policy_id)
    record = graph_writer.read_policy_tree(graph, policy_id)
    if record is None:
        raise PolicyNotFoundError(policy_id)
    return record


@dataclass(frozen=True, slots=True)
class _TransitionGate:
    """One already-evaluated precondition in `_apply_transition`'s own gate list (S26).

    Each transition function still builds its own `PolicyLifecycleRuleContext`
    and picks its own error type/message/`reason_code` -- that part
    genuinely differs per action, so it is not shared. `_apply_transition`
    only ever consumes the result: the first `not allowed` gate in the
    sequence it's handed determines the rejection.
    """

    allowed: bool
    reason_code: _TransitionReasonCode
    error: Exception


@dataclass(frozen=True, slots=True)
class _TransitionSpec:
    """The "what transition is this" facts `_apply_transition` needs (S26).

    Split out from `_apply_transition`'s own parameter list purely to stay
    under L2's `PLR0913` argument-count limit -- `action`/`from_status`/
    `target_status` are always supplied together by every call site (one
    literal triple per transition function), so grouping them costs no
    expressiveness.
    """

    action: str
    from_status: str
    target_status: str


def _apply_transition(
    *,
    actor: tuple[str, str],
    policy_id: str,
    spec: _TransitionSpec,
    affected_node_ids: tuple[str, ...],
    gates: Sequence[_TransitionGate],
    graph: GraphHandle,
    audit_store: AuditStore,
) -> None:
    """D-9's full gate-check + audit-then-cascade sequence, shared by every transition (S26).

    Extracted only after `propose_policy`/`approve_policy`/`reject_policy`/
    `revert_policy_to_draft` (plus `approve_policy`'s own auto-deprecation
    cascade, S25) were each independently green -- L1's "prefer duplication
    over the wrong abstraction" rule argues against extracting this before
    real call sites exist to prove the right shape; five call sites is well
    past that threshold. `create_policy_draft` is deliberately NOT routed
    through this helper: it is a creation, not a transition -- it has no
    prior status to gate on, no owner/role check to run, and its own
    title-collision read has a different shape than a `PolicyRecord` tree
    read (PLAN.md D-9/S26's own scoping).

    `gates` is evaluated in order; the first `not allowed` entry records an
    `outcome="rejected"` audit event (that gate's own `reason_code`, with
    `affected_node_ids` fixed to `(policy_id,)` -- nothing was ever
    transitioned, so only the root Policy id is named) then raises that
    gate's own named error -- byte-identical to every transition function's
    own pre-refactor if/raise chain. When every gate passes, delegates to
    `_cascade_with_audit` for the `outcome="applied"` event, the cascading
    write, and the `outcome="failed"` follow-up on a graph failure
    (AC-BI-024) -- unchanged from before this refactor.

    Args:
        actor: The calling caller's verified `(sub, iss)` identity.
        policy_id: The Policy id being transitioned.
        spec: The registered `policy.*` audit action name plus the
            before/after status pair -- `from_status` is used verbatim for a
            rejected event's `from_status`/`to_status` (a rejected call
            never actually changes status) and for the applied event's own
            `from_status`; `target_status` only for the applied event.
        affected_node_ids: `(policy_id, *standard_ids, *control_ids)` for
            the tree being transitioned -- only used once every gate has
            passed (the `applied`/`failed` events).
        gates: Every precondition for this transition, already evaluated by
            the caller, in the exact order the pre-refactor code checked
            them.
        graph: The single-tenant policy graph handle.
        audit_store: Where every audit event this call records is written,
            via `record_standalone`.

    Raises:
        Exception: whichever named error the first failing gate in `gates`
            carries.
        PolicyLifecycleGraphUnavailableError: the graph write failed after
            the `applied` audit event was already recorded (AC-BI-024).
    """
    actor_subject, actor_issuer = actor
    for gate in gates:
        if gate.allowed:
            continue
        audit_store.record_standalone(
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            action=spec.action,
            resource_type=_POLICY_RESOURCE_TYPE,
            resource_id=policy_id,
            outcome="rejected",
            details={
                "affected_node_ids": (policy_id,),
                "from_status": spec.from_status,
                "to_status": spec.from_status,
                "reason_code": gate.reason_code,
            },
        )
        raise gate.error

    _cascade_with_audit(
        actor=actor,
        action=spec.action,
        policy_id=policy_id,
        affected_node_ids=affected_node_ids,
        from_status=spec.from_status,
        target_status=spec.target_status,
        graph=graph,
        audit_store=audit_store,
    )


@dataclass(frozen=True, slots=True)
class PolicyApproveResult:
    """A successful `approve_policy` call's resulting state (issue #134, S17/S25)."""

    policy_id: str
    status: Literal["approved"]
    standard_ids: tuple[str, ...]
    control_ids: tuple[str, ...]
    auto_deprecated_policy_id: str | None


def approve_policy(
    *,
    actor: tuple[str, str],
    policy_id: str,
    graph: GraphHandle,
    audit_store: AuditStore,
    access_role_store: AccessRoleStore,
) -> PolicyApproveResult:
    """Approve `policy_id`, cascading its whole tree to `status="approved"` (S17).

    Ordering (D-9, mirroring `propose_policy` exactly): (0)
    `backfill_governance_status`; (1) a pure read
    (`graph_writer.read_policy_tree`) of the Policy's owner/status and every
    Standard/Control id in its tree; (2) RBAC gate --
    `ps_service.authz.service.require_role(actor,
    minimum=AccessRole.POLICY_MANAGER, store=access_role_store)`, reused
    verbatim -- it raises `AccessDeniedError` itself (AC-BI-008), so this
    function adds no audit-then-raise wrapping around it, exactly like
    PLAN.md's own instruction for this specific gate; (3) `block_self_approval`
    (AC-BI-006 -- a caller may never approve a Policy they own; AC-BI-017's
    same-subject-different-issuer case falls out for free from `(subject,
    issuer)` tuple equality, no special-casing); (4) `require_status
    (action="approve")` -- must be `"proposed"` (AC-BI-023); (5) on gate 3/4
    failure, a `outcome="rejected"` audit event (`reason_code`
    `"self_approval_blocked"`/`"invalid_status"`) is recorded before the
    named error is raised; (6) on all gates passing, `_cascade_with_audit`
    records the `outcome="applied"` event BEFORE the graph write, then issues
    `graph_writer.cascade_status(..., target_status="approved")`; (7) on a
    graph failure, `_cascade_with_audit` records a `outcome="failed"`
    follow-up event and raises `PolicyLifecycleGraphUnavailableError`
    (AC-BI-024); (8) **auto-deprecation** (D-10/S25): only once the
    successor's own cascade has succeeded, `graph_writer.find_approved_prior`
    checks for an inbound `SUPERSEDED_BY` edge from an `approved` prior -- if
    one exists, `_cascade_with_audit` runs again against the *prior's own*
    tree, target status `"deprecated"`, action `"policy.auto_deprecate"`,
    `resource_id` the prior's own id -- a second, wholly separate audit
    event, not folded into the successor's own `policy.approve` event
    (AC-BI-019). A `SUPERSEDED_BY`-linked prior that is not itself currently
    `"approved"` (still `"draft"`/`"proposed"`) is left untouched.

    Args:
        actor: The calling caller's verified `(sub, iss)` identity.
        policy_id: The Policy id to approve.
        graph: The single-tenant policy graph handle.
        audit_store: Where every `policy.approve`/`policy.auto_deprecate`
            audit event is recorded, via `record_standalone` -- this call has
            no surrounding state-changing transaction to join.
        access_role_store: Where `actor`'s active `AccessRole`s are resolved
            from, for the `PolicyManager` RBAC gate.

    Returns:
        A `PolicyApproveResult` describing the now-`"approved"` Policy, its
        full Standard/Control id tree, and (when an auto-deprecation
        cascade also ran) the auto-deprecated prior Policy's own id.

    Raises:
        PolicyNotFoundError: no `Policy` node exists with `policy_id`.
        AccessDeniedError: `actor` does not hold `PolicyManager`
            (`ps_service.api.errors.AccessDeniedError`, raised directly by
            `require_role`).
        PolicySelfApprovalBlockedError: `actor` is `policy_id`'s own owner.
        PolicyInvalidStatusTransitionError: `policy_id`'s Policy is not
            currently `"proposed"`.
        PolicyLifecycleGraphUnavailableError: a graph write failed after its
            own `applied` audit event was already recorded -- a `failed`
            follow-up event is recorded for that same action/resource_id
            before this is raised (AC-BI-024).
    """
    record = _read_transition_target(graph, policy_id)

    owner = (record.owner_subject, record.owner_issuer)
    current_status = cast("PolicyStatus", record.status)
    affected_node_ids = _tree_node_ids(record)
    context = PolicyLifecycleRuleContext(
        actor=actor, owner=owner, action="approve", current_status=current_status
    )

    # (2) RBAC gate -- raises `AccessDeniedError` itself; no audit-then-raise
    # wrapping needed for this specific gate (PLAN.md S17's own instruction)
    # -- deliberately not one of `_apply_transition`'s own `gates` below.
    require_role(actor, minimum=AccessRole.POLICY_MANAGER, store=access_role_store)

    gates = (
        _TransitionGate(
            allowed=block_self_approval(context).allowed,
            reason_code="self_approval_blocked",
            error=PolicySelfApprovalBlockedError(),
        ),
        _TransitionGate(
            allowed=require_status(context).allowed,
            reason_code="invalid_status",
            error=PolicyInvalidStatusTransitionError(
                action="approve", current_status=current_status, required_status="proposed"
            ),
        ),
    )
    _apply_transition(
        actor=actor,
        policy_id=policy_id,
        spec=_TransitionSpec(
            action=_APPROVE_ACTION, from_status=current_status, target_status="approved"
        ),
        affected_node_ids=affected_node_ids,
        gates=gates,
        graph=graph,
        audit_store=audit_store,
    )

    auto_deprecated_policy_id: str | None = None
    prior_id = graph_writer.find_approved_prior(graph, policy_id)
    if prior_id is not None:
        prior_record = graph_writer.read_policy_tree(graph, prior_id)
        if prior_record is not None:
            _cascade_with_audit(
                actor=actor,
                action=_AUTO_DEPRECATE_ACTION,
                policy_id=prior_id,
                affected_node_ids=_tree_node_ids(prior_record),
                from_status=prior_record.status,
                target_status="deprecated",
                graph=graph,
                audit_store=audit_store,
            )
            auto_deprecated_policy_id = prior_id

    return PolicyApproveResult(
        policy_id=policy_id,
        status="approved",
        standard_ids=tuple(standard.id for standard in record.standards),
        control_ids=tuple(
            control.id for standard in record.standards for control in standard.controls
        ),
        auto_deprecated_policy_id=auto_deprecated_policy_id,
    )


@dataclass(frozen=True, slots=True)
class PolicyRejectResult:
    """A successful `reject_policy` call's resulting state (issue #134, S19)."""

    policy_id: str
    status: Literal["draft"]
    standard_ids: tuple[str, ...]
    control_ids: tuple[str, ...]


def reject_policy(
    *,
    actor: tuple[str, str],
    policy_id: str,
    graph: GraphHandle,
    audit_store: AuditStore,
    access_role_store: AccessRoleStore,
) -> PolicyRejectResult:
    """Reject `policy_id`, cascading its whole tree back to `status="draft"` (S19).

    Ordering (D-9, an IDENTICAL gate stack to `approve_policy` -- same RBAC
    gate, same `block_self_approval` rule, same `require_status` check --
    just cascading to `"draft"` instead of `"approved"`, with no
    auto-deprecation call (D-10 is approve-only)): (0)
    `backfill_governance_status`; (1) a pure read
    (`graph_writer.read_policy_tree`) of the Policy's owner/status and every
    Standard/Control id in its tree; (2) RBAC gate --
    `ps_service.authz.service.require_role(actor,
    minimum=AccessRole.POLICY_MANAGER, store=access_role_store)`, reused
    verbatim -- it raises `AccessDeniedError` itself (AC-BI-008), so this
    function adds no audit-then-raise wrapping around it; (3)
    `block_self_approval` (AC-BI-006 -- a caller may never reject a Policy
    they own; AC-BI-017's same-subject-different-issuer case falls out for
    free from `(subject, issuer)` tuple equality); (4) `require_status
    (action="reject")` -- must be `"proposed"` (AC-BI-023); (5) on gate 3/4
    failure, a `outcome="rejected"` audit event (`reason_code`
    `"self_approval_blocked"`/`"invalid_status"`) is recorded before the
    named error is raised; (6) on all gates passing, `_cascade_with_audit`
    records the `outcome="applied"` event BEFORE the graph write, then issues
    `graph_writer.cascade_status(..., target_status="draft")` (AC-BI-005);
    (7) on a graph failure, `_cascade_with_audit` records a `outcome="failed"`
    follow-up event and raises `PolicyLifecycleGraphUnavailableError`
    (AC-BI-024).

    Args:
        actor: The calling caller's verified `(sub, iss)` identity.
        policy_id: The Policy id to reject.
        graph: The single-tenant policy graph handle.
        audit_store: Where every `policy.reject` audit event is recorded, via
            `record_standalone` -- this call has no surrounding
            state-changing transaction to join.
        access_role_store: Where `actor`'s active `AccessRole`s are resolved
            from, for the `PolicyManager` RBAC gate.

    Returns:
        A `PolicyRejectResult` describing the now-`"draft"` Policy and its
        full Standard/Control id tree.

    Raises:
        PolicyNotFoundError: no `Policy` node exists with `policy_id`.
        AccessDeniedError: `actor` does not hold `PolicyManager`
            (`ps_service.api.errors.AccessDeniedError`, raised directly by
            `require_role`).
        PolicySelfApprovalBlockedError: `actor` is `policy_id`'s own owner.
        PolicyInvalidStatusTransitionError: `policy_id`'s Policy is not
            currently `"proposed"`.
        PolicyLifecycleGraphUnavailableError: a graph write failed after its
            own `applied` audit event was already recorded -- a `failed`
            follow-up event is recorded for that same action/resource_id
            before this is raised (AC-BI-024).
    """
    record = _read_transition_target(graph, policy_id)

    owner = (record.owner_subject, record.owner_issuer)
    current_status = cast("PolicyStatus", record.status)
    affected_node_ids = _tree_node_ids(record)
    context = PolicyLifecycleRuleContext(
        actor=actor, owner=owner, action="reject", current_status=current_status
    )

    # (2) RBAC gate -- raises `AccessDeniedError` itself; no audit-then-raise
    # wrapping needed for this specific gate (mirrors `approve_policy`, S17)
    # -- deliberately not one of `_apply_transition`'s own `gates` below.
    require_role(actor, minimum=AccessRole.POLICY_MANAGER, store=access_role_store)

    gates = (
        _TransitionGate(
            allowed=block_self_approval(context).allowed,
            reason_code="self_approval_blocked",
            error=PolicySelfApprovalBlockedError(),
        ),
        _TransitionGate(
            allowed=require_status(context).allowed,
            reason_code="invalid_status",
            error=PolicyInvalidStatusTransitionError(
                action="reject", current_status=current_status, required_status="proposed"
            ),
        ),
    )
    _apply_transition(
        actor=actor,
        policy_id=policy_id,
        spec=_TransitionSpec(
            action=_REJECT_ACTION, from_status=current_status, target_status="draft"
        ),
        affected_node_ids=affected_node_ids,
        gates=gates,
        graph=graph,
        audit_store=audit_store,
    )

    return PolicyRejectResult(
        policy_id=policy_id,
        status="draft",
        standard_ids=tuple(standard.id for standard in record.standards),
        control_ids=tuple(
            control.id for standard in record.standards for control in standard.controls
        ),
    )


@dataclass(frozen=True, slots=True)
class PolicyRevertResult:
    """A successful `revert_policy_to_draft` call's resulting state (issue #134, S21)."""

    policy_id: str
    status: Literal["draft"]
    standard_ids: tuple[str, ...]
    control_ids: tuple[str, ...]


def revert_policy_to_draft(
    *,
    actor: tuple[str, str],
    policy_id: str,
    graph: GraphHandle,
    audit_store: AuditStore,
) -> PolicyRevertResult:
    """Revert `policy_id`, cascading its whole tree back to `status="draft"` (S21).

    Structurally DISTINCT from `approve_policy`/`reject_policy`: this action
    is OWNER-only, with no `PolicyManager` RBAC gate at all -- a non-owner,
    INCLUDING a `PolicyManager` who is not the owner, is rejected
    (AC-BI-002/007 -- this proves the action is genuinely owner-only, not
    role-gated).

    Ordering (D-9, mirroring `propose_policy`'s owner-only shape): (0)
    `backfill_governance_status`; (1) a pure read
    (`graph_writer.read_policy_tree`) of the Policy's owner/status and every
    Standard/Control id in its tree; (2) `require_owner` (AC-BI-002/007 --
    only the Policy's own owner may revert it); (3) `require_status
    (action="revert")` -- must be `"proposed"` (AC-BI-023); (4) on gate 2/3
    failure, a `outcome="rejected"` audit event (`reason_code`
    `"access_denied"`/`"invalid_status"`) is recorded before the named error
    is raised; (5) on all gates passing, `_cascade_with_audit` records the
    `outcome="applied"` event BEFORE the graph write, then issues
    `graph_writer.cascade_status(..., target_status="draft")`; (6) on a graph
    failure, `_cascade_with_audit` records a `outcome="failed"` follow-up
    event and raises `PolicyLifecycleGraphUnavailableError` (AC-BI-024).

    Args:
        actor: The calling caller's verified `(sub, iss)` identity.
        policy_id: The Policy id to revert.
        graph: The single-tenant policy graph handle.
        audit_store: Where every `policy.revert` audit event is recorded, via
            `record_standalone` -- this call has no surrounding
            state-changing transaction to join.

    Returns:
        A `PolicyRevertResult` describing the now-`"draft"` Policy and its
        full Standard/Control id tree.

    Raises:
        PolicyNotFoundError: no `Policy` node exists with `policy_id`.
        PolicyDraftAccessDeniedError: `actor` is not `policy_id`'s owner --
            including when `actor` holds `PolicyManager` but is not the
            owner.
        PolicyInvalidStatusTransitionError: `policy_id`'s Policy is not
            currently `"proposed"`.
        PolicyLifecycleGraphUnavailableError: a graph write failed after its
            own `applied` audit event was already recorded -- a `failed`
            follow-up event is recorded for that same action/resource_id
            before this is raised (AC-BI-024).
    """
    record = _read_transition_target(graph, policy_id)

    owner = (record.owner_subject, record.owner_issuer)
    current_status = cast("PolicyStatus", record.status)
    affected_node_ids = _tree_node_ids(record)
    context = PolicyLifecycleRuleContext(
        actor=actor, owner=owner, action="revert", current_status=current_status
    )

    gates = (
        _TransitionGate(
            allowed=require_owner(context).allowed,
            reason_code="access_denied",
            error=PolicyDraftAccessDeniedError(),
        ),
        _TransitionGate(
            allowed=require_status(context).allowed,
            reason_code="invalid_status",
            error=PolicyInvalidStatusTransitionError(
                action="revert", current_status=current_status, required_status="proposed"
            ),
        ),
    )
    _apply_transition(
        actor=actor,
        policy_id=policy_id,
        spec=_TransitionSpec(
            action=_REVERT_ACTION, from_status=current_status, target_status="draft"
        ),
        affected_node_ids=affected_node_ids,
        gates=gates,
        graph=graph,
        audit_store=audit_store,
    )

    return PolicyRevertResult(
        policy_id=policy_id,
        status="draft",
        standard_ids=tuple(standard.id for standard in record.standards),
        control_ids=tuple(
            control.id for standard in record.standards for control in standard.controls
        ),
    )
