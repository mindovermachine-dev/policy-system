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
    PolicyCapabilityAlreadyGovernedError,
    PolicyCapabilityNotFoundError,
    PolicyControlNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyGovernanceConflictError,
    PolicyIncompleteForProposalError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
    PolicyNotFoundError,
    PolicySelfApprovalBlockedError,
    PolicyStandardNotFoundError,
    PolicySupersedePriorNotApprovedError,
    PolicyTitleAlreadyExistsError,
)
from ps_service.policy_lifecycle.rules import (
    PolicyLifecycleRuleContext,
    block_self_approval,
    require_owner,
    require_status,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
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
    "ControlDraftResult",
    "ControlView",
    "PolicyApproveResult",
    "PolicyDraftResult",
    "PolicyDraftUpdateResult",
    "PolicyProposeResult",
    "PolicyRejectResult",
    "PolicyRevertResult",
    "PolicyView",
    "StandardDraftInput",
    "StandardDraftResult",
    "StandardView",
    "add_control_to_draft",
    "add_standard_to_draft",
    "approve_policy",
    "create_policy_draft",
    "get_policy",
    "propose_policy",
    "reject_policy",
    "revert_policy_to_draft",
    "update_control_draft",
    "update_policy_draft",
    "update_standard_draft",
]

# `AccessRole`s that see a Draft Policy regardless of ownership (issue #134, S13).
_DRAFT_VISIBILITY_OVERRIDE_ROLES = frozenset({AccessRole.SYSTEM_OWNER, AccessRole.SYSTEM_ADMIN})

# Issue #136, Slice 1, §1.6: `update-policy-draft`'s patchable-field allow-list,
# from `ps-domain-concepts.md`'s Policy property table. Deliberately excludes
# `title` (identity-bearing -- `policy_id` is content-derived from it),
# `status` (lifecycle-managed only, via #134's propose/approve/reject/revert),
# and `owner_subject`/`owner_issuer`/`version` (identity/lifecycle-managed).
# Declared once here and imported into `mcp_interface.mcp_server` (its own
# `_parse_patch_fields` call site) to avoid drift between the two layers.
_POLICY_PATCHABLE_FIELDS = frozenset(
    {
        "description",
        "scope_in",
        "scope_out",
        "normative_commitments",
        "review_cadence",
        "exception_pathway",
        "measurable_outcomes",
        "capability_grouping_rationale",
    }
)

# Issue #136, Slice 2, §1.6: `add-standard-to-draft`'s (and the future
# `update-standard-draft`'s) patchable-field allow-list, from
# `ps-domain-concepts.md`'s Standard property table. Deliberately excludes
# `title` (identity-bearing -- `standard_id` is content-derived from it) and
# `status` (governance status -- lifecycle-managed only, never set through
# this issue's content tools). `implementation_status` IS included here
# (unlike Policy's `version`/`status`) -- it is Standard's own workflow
# field, distinct from governance `status`, and AC-BI-007 requires it be
# independently settable.
_STANDARD_PATCHABLE_FIELDS = frozenset(
    {
        "description",
        "implementation_status",
        "procedure",
        "implementer_role",
        "reviewer_role",
        "applicability_boundary",
        "verification_notes",
        "change_rationale",
    }
)
_STANDARD_IMPLEMENTATION_STATUS_VALUES = ("draft", "implemented", "reviewed", "deprecated")

# Issue #136, Slice 4, §1.6: `add-control-to-draft`'s (and the future
# `update-control-draft`'s) patchable-field allow-list, from
# `ps-domain-concepts.md`'s Control property table. Deliberately excludes
# `title` (identity-bearing -- `control_id` is content-derived from it) and
# `status` (governance status -- lifecycle-managed only). Unlike
# `_STANDARD_PATCHABLE_FIELDS`, this includes `"type"` -- CHANGES.md finding
# #8: `add-control-to-draft` itself excludes `"type"` from ITS OWN allow-list
# at the MCP boundary (the top-level `control_type` param is the only way to
# set it at creation), but `update-control-draft` (Slice 5) needs the full
# set including `"type"` as the only post-creation path to change it. This
# constant is the full set; `mcp_interface.mcp_server`'s own
# `_ADD_CONTROL_TO_DRAFT_PATCHABLE_FIELDS` narrows it by one key for that
# tool alone.
_CONTROL_PATCHABLE_FIELDS = frozenset(
    {
        "description",
        "implementation_status",
        "type",
        "execution_frequency",
        "last_test_date",
        "next_review_date",
        "evidence_ref",
        "pass_fail_criteria",
        "execution_method",
        "evidence_plan",
        "executor_role",
        "reviewer_role",
        "risk_alignment_rationale",
    }
)
_CONTROL_IMPLEMENTATION_STATUS_VALUES = ("planned", "implemented", "reviewed", "deprecated")

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
    """The newly-minted draft Policy's identity and initial state.

    `version` (issue #136, Slice 6, CHANGES.md finding #1): widened from
    `Literal["1"]` to `str` -- an ordinary draft still gets `"1"`, but a
    supersede fork's successor version is `str(int(prior_version) + 1)`,
    genuinely variable. `superseded_policy_id` is `None` for an ordinary
    draft, or the forked-from Policy's id (AC-BI-010). `capability_ids`
    (issue #185) are the Capabilities the fresh draft now governs; `()` for
    a fork or a draft created without any.
    """

    policy_id: str
    title: str
    status: Literal["draft"]
    version: str
    owner_subject: str
    owner_issuer: str
    standard_ids: tuple[str, ...]
    control_ids: tuple[str, ...]
    superseded_policy_id: str | None = None
    capability_ids: tuple[str, ...] = ()


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


# Property keys never carried over as a forked node's own `extra_properties`
# (issue #136, Slice 6): `id`/`status` are always freshly computed/forced by
# the fork itself (a new node's id always differs from its source's; a
# forked node's governance status is always `"draft"`, never copied from the
# source, which is `"approved"` -- AC-BI-007), `title` is carried separately
# via each `Forked*Record.title` field. `type` is stripped from a forked
# Control's own `extra_properties` too -- `graph_writer.create_policy_draft`
# always sets it from `ControlDraft.control_type` (never from
# `extra_properties`), mirroring `add_control_to_standard`'s own defence-in-
# depth discipline (CHANGES.md finding #8).
_FORKED_STANDARD_STRIPPED_KEYS = frozenset({"id", "title", "status"})
_FORKED_CONTROL_STRIPPED_KEYS = frozenset({"id", "title", "status", "type"})


def _build_forked_standard_drafts(
    new_policy_id: str, prior_policy_id: str, graph: GraphHandle
) -> tuple[graph_writer.StandardDraft, ...]:
    """Fork `prior_policy_id`'s current Standard/Control tree onto `new_policy_id` (Slice 6).

    Reads the prior tree's FULL content via `graph_writer.read_policy_tree_for_fork`
    (G7 -- `read_policy_tree`'s own `StandardRecord`/`ControlRecord` are too
    narrow, id/title/status only) and computes every forked child's own new,
    independent id up front (`standard_id(new_policy_id, ...)`/
    `control_id(new_standard_id, ...)` -- the SAME id formulas every ordinary
    creation path uses, mirroring `_build_standard_drafts`'s own "compute
    ids before either audit write or the graph write" discipline), so both
    the `applied` audit event's `affected_node_ids` and the eventual graph
    write use the exact same ids.

    AC-BI-006 (the prior tree is never mutated): this function is READ-ONLY
    against the prior -- it never calls any `graph_writer` write function
    against `prior_policy_id` or any of its own Standard/Control ids; every
    `StandardDraft`/`ControlDraft` this returns carries a brand-new id under
    `new_policy_id`.
    """
    forked_standards = graph_writer.read_policy_tree_for_fork(graph, prior_policy_id)
    drafts: list[graph_writer.StandardDraft] = []
    for standard in forked_standards:
        new_standard_id = standard_id(new_policy_id, standard.title)
        controls = tuple(
            graph_writer.ControlDraft(
                id=control_id(new_standard_id, control.title),
                title=control.title,
                control_type=cast("str", control.properties.get("type", "manual")),
                extra_properties={
                    key: value
                    for key, value in control.properties.items()
                    if key not in _FORKED_CONTROL_STRIPPED_KEYS
                },
            )
            for control in standard.controls
        )
        drafts.append(
            graph_writer.StandardDraft(
                id=new_standard_id,
                title=standard.title,
                controls=controls,
                extra_properties={
                    key: value
                    for key, value in standard.properties.items()
                    if key not in _FORKED_STANDARD_STRIPPED_KEYS
                },
            )
        )
    return tuple(drafts)


def _validate_capability_claims(
    *,
    graph: GraphHandle,
    audit_store: AuditStore,
    actor: tuple[str, str],
    new_policy_id: str,
    capability_ids: tuple[str, ...],
) -> None:
    """Reject a fresh draft's claim on missing or already-governed Capabilities (issue #185).

    Pure read (`graph_writer.read_capability_governors`); on a bad claim a
    `rejected` `policy.create_draft` audit event (with `capability_ids=()`,
    nothing is claimed) is recorded BEFORE the named error is raised, and no
    graph write is ever attempted.
    """
    governors = graph_writer.read_capability_governors(graph, capability_ids)
    missing = tuple(cap for cap in capability_ids if cap not in governors)
    governed = tuple(cap for cap in capability_ids if governors.get(cap) is not None)
    if not missing and not governed:
        return
    _record_create_draft(
        audit_store,
        actor=actor,
        resource_id=new_policy_id,
        outcome="rejected",
        details={
            "affected_node_ids": (),
            "to_status": "draft",
            "reason_code": "capability_not_found" if missing else "capability_already_governed",
            "capability_ids": (),
        },
    )
    if missing:
        raise PolicyCapabilityNotFoundError(missing)
    raise PolicyCapabilityAlreadyGovernedError(governed)


def _record_create_draft(
    audit_store: AuditStore,
    *,
    actor: tuple[str, str],
    resource_id: str,
    outcome: Literal["applied", "rejected", "failed"],
    details: Mapping[str, object],
) -> None:
    """Record one `policy.create_draft` audit event (the component's semantic log)."""
    audit_store.record_standalone(
        actor_subject=actor[0],
        actor_issuer=actor[1],
        action=_CREATE_DRAFT_ACTION,
        resource_type=_POLICY_RESOURCE_TYPE,
        resource_id=resource_id,
        outcome=outcome,
        details=details,
    )


def create_policy_draft(
    *,
    actor: tuple[str, str],
    title: str,
    standards: tuple[StandardDraftInput, ...] = (),
    supersedes_policy_id: str | None = None,
    capability_ids: tuple[str, ...] = (),
    graph: GraphHandle,
    audit_store: AuditStore,
) -> PolicyDraftResult:
    """Create a new draft Policy owned by `actor` (AC-BI-001/009/015/016/022/024).

    `supersedes_policy_id` (issue #136, Slice 6 -- the amendment fork): when
    given, this call mints a successor draft instead of a v1 Policy. Ordering
    (extends D-9, applied before the existing title-collision check): (0) if
    `supersedes_policy_id` is set, `_read_transition_target` reads the prior
    Policy (backfill + read + `PolicyNotFoundError` if it doesn't exist,
    reused unchanged, G11) and its `status` must be `"approved"`
    (`PolicySupersedePriorNotApprovedError` otherwise, AC-BI-011) -- no audit
    event is recorded for either rejection, and no graph write is ever
    attempted; (1) the successor id is `policy_id(title, prior_policy_id)`
    and its `version` is `str(int(prior_version) + 1)` (CHANGES.md finding
    #1 -- string-typed arithmetic, no schema migration); (2) its Standard/
    Control children are the prior tree's own current content, forked as
    brand-new, independently-editable nodes (`_build_forked_standard_drafts`)
    instead of any caller-supplied `standards` -- `standards` is silently
    ignored when `supersedes_policy_id` is set (documented in the tool's own
    docstring, not a separate validation error); (3) the graph write also
    creates a single Policy-level `(prior)-[:SUPERSEDED_BY]->(new)` edge, no
    per-Standard/Control lineage edges (AC-BI-005); (4) the prior tree's own
    nodes are never mutated (AC-BI-006 -- `_build_forked_standard_drafts` is
    read-only against the prior, and the graph write only ever `MERGE`s onto
    the new tree's own freshly-computed ids). The existing `policy.
    create_draft` audit call (both its `applied` and `failed` events) is
    reused unchanged in shape, only gaining `supersedes_policy_id` in
    `details` (present-but-`None` on an ordinary draft, populated on a fork)
    -- no second/separate audit call is added for the fork (AC-BI-014).

    Args:
        actor: The creating caller's verified `(sub, iss)` identity --
            becomes the new Policy's `owner_subject`/`owner_issuer`.
        title: The new Policy's title (feeds `policy_id(title)`, v1, or
            `policy_id(title, supersedes_policy_id)` for a fork).
        standards: Optional Standard children (each with its own optional
            Control children) to mint alongside the Policy, all
            unconditionally `status="draft"` (D-6). Ignored when
            `supersedes_policy_id` is set.
        supersedes_policy_id: An existing, `"approved"` Policy id to fork a
            successor draft from, or `None` for an ordinary v1 draft.
        capability_ids: Capabilities a FRESH draft claims via `GOVERNED_BY`
            at creation (issue #185, de-duplicated order-preserving).
            Silently ignored when `supersedes_policy_id` is set -- a fork's
            edges stay on the prior Policy until it is approved.
        graph: The single-tenant policy graph handle.
        audit_store: Where every `policy.create_draft` audit event is
            recorded, via `record_standalone` -- this call has no
            surrounding state-changing transaction to join.

    Returns:
        A `PolicyDraftResult` describing the newly-minted Policy.

    Raises:
        PolicyNotFoundError: `supersedes_policy_id` names a Policy that does
            not exist (AC-BI-011).
        PolicySupersedePriorNotApprovedError: `supersedes_policy_id` names a
            Policy that exists but is not currently `"approved"` (AC-BI-011).
        PolicyTitleAlreadyExistsError: the computed id already exists
            (AC-BI-022) -- a rejected audit event is recorded first; no
            graph write is ever attempted.
        PolicyCapabilityNotFoundError: a `capability_ids` entry names no
            Capability (issue #185) -- rejected audit event, no write.
        PolicyCapabilityAlreadyGovernedError: a `capability_ids` entry is
            already governed, or was claimed concurrently (the guarded write
            matched nothing -- `applied` then `failed` audit events).
        PolicyLifecycleGraphUnavailableError: the graph write failed after
            the `applied` audit event was already recorded -- a `failed`
            follow-up event is recorded for the same action/resource_id
            before this is raised (AC-BI-024).
    """
    owner_subject, owner_issuer = actor

    new_version = "1"
    if supersedes_policy_id is not None:
        prior_record = _read_transition_target(graph, supersedes_policy_id)
        if prior_record.status != "approved":
            raise PolicySupersedePriorNotApprovedError(supersedes_policy_id, prior_record.status)
        new_version = str(int(prior_record.version) + 1)
    new_policy_id = compute_policy_id(title, prior_policy_id=supersedes_policy_id)

    existing = graph_writer.find_existing_policy(graph, new_policy_id)
    if existing is not None:
        existing_id, _existing_title = existing
        _record_create_draft(
            audit_store,
            actor=actor,
            resource_id=new_policy_id,
            outcome="rejected",
            details={
                "affected_node_ids": (),
                "to_status": "draft",
                "reason_code": "title_already_exists",
            },
        )
        raise PolicyTitleAlreadyExistsError(title, existing_id)

    claimed_capability_ids: tuple[str, ...] = (
        () if supersedes_policy_id is not None else tuple(dict.fromkeys(capability_ids))
    )
    if claimed_capability_ids:
        _validate_capability_claims(
            graph=graph,
            audit_store=audit_store,
            actor=actor,
            new_policy_id=new_policy_id,
            capability_ids=claimed_capability_ids,
        )

    standard_drafts = (
        _build_forked_standard_drafts(new_policy_id, supersedes_policy_id, graph)
        if supersedes_policy_id is not None
        else _build_standard_drafts(new_policy_id, standards)
    )
    new_standard_ids = tuple(standard.id for standard in standard_drafts)
    new_control_ids = tuple(
        control.id for standard in standard_drafts for control in standard.controls
    )
    affected_node_ids = (new_policy_id, *new_standard_ids, *new_control_ids)

    _record_create_draft(
        audit_store,
        actor=actor,
        resource_id=new_policy_id,
        outcome="applied",
        details={
            "affected_node_ids": affected_node_ids,
            "to_status": "draft",
            "supersedes_policy_id": supersedes_policy_id,
            "capability_ids": claimed_capability_ids,
        },
    )

    try:
        created = graph_writer.create_policy_draft(
            graph,
            policy_id=new_policy_id,
            title=title,
            owner=actor,
            standards=standard_drafts,
            supersedes_policy_id=supersedes_policy_id,
            version=new_version,
            capability_ids=claimed_capability_ids,
        )
    except redis.exceptions.RedisError as exc:
        _record_create_draft(
            audit_store,
            actor=actor,
            resource_id=new_policy_id,
            outcome="failed",
            details={
                "affected_node_ids": affected_node_ids,
                "to_status": "draft",
                "supersedes_policy_id": supersedes_policy_id,
                "capability_ids": claimed_capability_ids,
            },
        )
        raise PolicyLifecycleGraphUnavailableError from exc

    if not created:
        _record_create_draft(
            audit_store,
            actor=actor,
            resource_id=new_policy_id,
            outcome="failed",
            details={
                "affected_node_ids": (),
                "to_status": "draft",
                "reason_code": "capability_already_governed",
                "capability_ids": (),
            },
        )
        raise PolicyCapabilityAlreadyGovernedError(claimed_capability_ids)

    return PolicyDraftResult(
        policy_id=new_policy_id,
        title=title,
        status="draft",
        version=new_version,
        owner_subject=owner_subject,
        owner_issuer=owner_issuer,
        standard_ids=new_standard_ids,
        control_ids=new_control_ids,
        superseded_policy_id=supersedes_policy_id,
        capability_ids=claimed_capability_ids,
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


@dataclass(frozen=True, slots=True)
class _Repoint:
    """A fork approval's governed-Capability move (issue #185): from `prior_id` to the fork."""

    prior_id: str
    capability_ids: tuple[str, ...]


def _write_cascade(
    graph: GraphHandle, *, policy_id: str, target_status: str, repoint: _Repoint | None
) -> bool:
    """Issue the one status-cascade write; `False` only when a re-point guard failed."""
    if repoint is None:
        graph_writer.cascade_status(graph, policy_id=policy_id, target_status=target_status)
        return True
    return graph_writer.approve_fork_repoint(
        graph,
        policy_id=policy_id,
        prior_id=repoint.prior_id,
        capability_ids=repoint.capability_ids,
        target_status=target_status,
    )


def _cascade_with_audit(
    *,
    actor: tuple[str, str],
    policy_id: str,
    spec: _TransitionSpec,
    affected_node_ids: tuple[str, ...],
    graph: GraphHandle,
    audit_store: AuditStore,
    repoint: _Repoint | None = None,
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

    When `repoint` is given (issue #185: approving a fork whose prior governs
    Capabilities) the cascade is the single guarded re-point statement and the
    `applied` event carries the intended `capability_ids`. A guard mismatch
    records a `failed` event (`reason_code="governance_conflict"`,
    `capability_ids=()` -- nothing moved) and raises
    `PolicyGovernanceConflictError`.

    Raises:
        PolicyLifecycleGraphUnavailableError: the graph write failed after
            the `applied` audit event was already recorded -- a `failed`
            follow-up event is recorded for the same action/resource_id
            before this is raised (AC-BI-024), the original
            `redis.exceptions.RedisError` chained.
        PolicyGovernanceConflictError: `repoint` was given and the governed
            set no longer matched at write time; nothing was written.
    """
    actor_subject, actor_issuer = actor
    base_details: dict[str, object] = {
        "affected_node_ids": affected_node_ids,
        "from_status": spec.from_status,
        "to_status": spec.target_status,
    }
    applied_details = dict(base_details)
    if repoint is not None:
        applied_details["capability_ids"] = repoint.capability_ids

    def _record(outcome: Literal["applied", "failed"], details: dict[str, object]) -> None:
        audit_store.record_standalone(
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            action=spec.action,
            resource_type=_POLICY_RESOURCE_TYPE,
            resource_id=policy_id,
            outcome=outcome,
            details=details,
        )

    _record("applied", applied_details)
    try:
        moved = _write_cascade(
            graph, policy_id=policy_id, target_status=spec.target_status, repoint=repoint
        )
    except redis.exceptions.RedisError as exc:
        _record("failed", base_details)
        raise PolicyLifecycleGraphUnavailableError from exc
    if not moved:
        _record(
            "failed",
            {**base_details, "reason_code": "governance_conflict", "capability_ids": ()},
        )
        raise PolicyGovernanceConflictError(policy_id)


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
    """Run `_check_gates`, then `_cascade_with_audit` -- see `_check_gates` for the gate rules."""
    _check_gates(actor=actor, policy_id=policy_id, spec=spec, gates=gates, audit_store=audit_store)
    _cascade_with_audit(
        actor=actor,
        policy_id=policy_id,
        spec=spec,
        affected_node_ids=affected_node_ids,
        graph=graph,
        audit_store=audit_store,
    )


def _check_gates(
    *,
    actor: tuple[str, str],
    policy_id: str,
    spec: _TransitionSpec,
    gates: Sequence[_TransitionGate],
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
    own pre-refactor if/raise chain. When every gate passes this returns and
    the caller runs `_cascade_with_audit` (`_apply_transition` does so
    directly; `approve_policy` first reads the fork's governed Capabilities,
    which must not happen before a gate has rejected).

    Args:
        actor: The calling caller's verified `(sub, iss)` identity.
        policy_id: The Policy id being transitioned.
        spec: The registered `policy.*` audit action name plus the
            before/after status pair -- `from_status` is used verbatim for a
            rejected event's `from_status`/`to_status` (a rejected call
            never actually changes status) and for the applied event's own
            `from_status`; `target_status` only for the applied event.
        gates: Every precondition for this transition, already evaluated by
            the caller, in the exact order the pre-refactor code checked
            them.
        audit_store: Where every rejected audit event is written, via
            `record_standalone`.

    Raises:
        Exception: whichever named error the first failing gate in `gates`
            carries.
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


@dataclass(frozen=True, slots=True)
class PolicyApproveResult:
    """A successful `approve_policy` call's resulting state (issue #134, S17/S25)."""

    policy_id: str
    status: Literal["approved"]
    standard_ids: tuple[str, ...]
    control_ids: tuple[str, ...]
    auto_deprecated_policy_id: str | None
    governed_capability_ids: tuple[str, ...] = ()


def _read_repoint(graph: GraphHandle, policy_id: str) -> _Repoint | None:
    """The fork's governed-Capability move, or `None` for the plain cascade (issue #185, D-5).

    `None` for a non-fork and for a fork whose prior governs nothing (legacy
    forks without `GOVERNED_BY` edges stay approvable).
    """
    governance = graph_writer.read_fork_governance(graph, policy_id)
    if governance is None or not governance.capability_ids:
        return None
    return _Repoint(prior_id=governance.prior_id, capability_ids=governance.capability_ids)


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
        PolicyGovernanceConflictError: the fork's prior governs Capabilities
            (issue #185) and that set changed between the read and the single
            guarded write; nothing was moved or approved. A `failed` audit
            event (`reason_code="governance_conflict"`) follows the `applied`
            one.
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
    spec = _TransitionSpec(
        action=_APPROVE_ACTION, from_status=current_status, target_status="approved"
    )
    _check_gates(actor=actor, policy_id=policy_id, spec=spec, gates=gates, audit_store=audit_store)
    repoint = _read_repoint(graph, policy_id)
    _cascade_with_audit(
        actor=actor,
        policy_id=policy_id,
        spec=spec,
        affected_node_ids=affected_node_ids,
        graph=graph,
        audit_store=audit_store,
        repoint=repoint,
    )

    auto_deprecated_policy_id: str | None = None
    prior_id = graph_writer.find_approved_prior(graph, policy_id)
    if prior_id is not None:
        prior_record = graph_writer.read_policy_tree(graph, prior_id)
        if prior_record is not None:
            _cascade_with_audit(
                actor=actor,
                policy_id=prior_id,
                spec=_TransitionSpec(
                    action=_AUTO_DEPRECATE_ACTION,
                    from_status=prior_record.status,
                    target_status="deprecated",
                ),
                affected_node_ids=_tree_node_ids(prior_record),
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
        governed_capability_ids=repoint.capability_ids if repoint is not None else (),
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


def _authorize_draft_edit(
    context: PolicyLifecycleRuleContext, *, access_role_store: AccessRoleStore
) -> None:
    """AC-BI-002/003 (owner-or-SystemOwner/SystemAdmin) then AC-BI-004 (must be draft).

    Issue #136 (PLAN.md §1.3): the shared owner-or-elevated + status gate
    every one of the six draft-content tools calls before doing anything
    else. Mirrors `get_policy`'s own override branch (§333-394 above)
    exactly, generalized from that one read-only visibility check to every
    draft-content mutation this issue introduces.

    Args:
        context: The lifecycle call's facts, built with `action="edit"`.
        access_role_store: Where `context.actor`'s active `AccessRole`s are
            resolved from, only ever consulted when `context.actor` is not
            the owner.

    Raises:
        PolicyDraftAccessDeniedError: `context.actor` is neither the
            owner nor a `SystemOwner`/`SystemAdmin`.
        PolicyInvalidStatusTransitionError: `context.current_status` is not
            `"draft"`.
    """
    if not require_owner(context).allowed:
        active_roles = resolve_active_roles(context.actor, store=access_role_store)
        if not (active_roles & _DRAFT_VISIBILITY_OVERRIDE_ROLES):
            raise PolicyDraftAccessDeniedError
    if not require_status(context).allowed:
        raise PolicyInvalidStatusTransitionError(
            action="edit", current_status=context.current_status, required_status="draft"
        )


@dataclass(frozen=True, slots=True)
class PolicyDraftUpdateResult:
    """A successful `update_policy_draft` call's resulting state (issue #136, Slice 1)."""

    policy_id: str
    updated_fields: tuple[str, ...]


def update_policy_draft(
    *,
    actor: tuple[str, str],
    policy_id: str,
    fields: Mapping[str, object],
    graph: GraphHandle,
    access_role_store: AccessRoleStore,
) -> PolicyDraftUpdateResult:
    """PATCH a subset of a draft Policy's own content fields (issue #136, Slice 1).

    Ordering: (1) `_read_transition_target` (backfill + read + not-found,
    reused unchanged from the four #134 transition functions); (2)
    `_authorize_draft_edit` (owner-or-elevated, then must be `"draft"`); (3)
    `graph_writer.update_policy_fields` applies exactly the supplied
    `fields`, nothing else (AC-BI-008 -- omitted fields keep their existing
    value; an explicitly-`None` value clears that field).

    Unlike every #134 transition function, this records no audit_events row
    at all (PLAN.md §1.1 -- only the Slice 6 supersede-fork path does that);
    it still gets the full `_run_mcp_action` started/succeeded/failed log
    triad for free at the MCP layer (AC-BI-014).

    Args:
        actor: The calling caller's verified `(sub, iss)` identity.
        policy_id: The draft Policy id to patch.
        fields: Field name -> new value (or `None` to clear), already
            restricted to `_POLICY_PATCHABLE_FIELDS` by the MCP-boundary
            parser -- this function does not re-validate keys.
        graph: The single-tenant policy graph handle.
        access_role_store: Where `actor`'s active `AccessRole`s are resolved
            from, only ever consulted when `actor` is not the Policy's
            owner.

    Returns:
        A `PolicyDraftUpdateResult` naming the Policy id and the fields
        that were actually supplied (sorted, for deterministic output).

    Raises:
        PolicyNotFoundError: no `Policy` node exists with `policy_id`.
        PolicyDraftAccessDeniedError: `actor` is neither `policy_id`'s owner
            nor a `SystemOwner`/`SystemAdmin`.
        PolicyInvalidStatusTransitionError: `policy_id`'s Policy is not
            currently `"draft"`.
        PolicyLifecycleGraphUnavailableError: the graph write itself failed
            (AC-BI-013's "graph unavailable" failure mode) -- the original
            `redis.exceptions.RedisError` is chained, never leaked to the
            caller. No audit event is recorded either way (§1.1 -- this
            tool never touches `audit_events` at all).
    """
    record = _read_transition_target(graph, policy_id)
    context = PolicyLifecycleRuleContext(
        actor=actor,
        owner=(record.owner_subject, record.owner_issuer),
        action="edit",
        current_status=cast("PolicyStatus", record.status),
    )
    _authorize_draft_edit(context, access_role_store=access_role_store)
    try:
        graph_writer.update_policy_fields(graph, policy_id=policy_id, properties=fields)
    except redis.exceptions.RedisError as exc:
        raise PolicyLifecycleGraphUnavailableError from exc
    return PolicyDraftUpdateResult(policy_id=policy_id, updated_fields=tuple(sorted(fields)))


@dataclass(frozen=True, slots=True)
class StandardDraftResult:
    """A successful `add_standard_to_draft` call's resulting state (issue #136, Slice 2)."""

    standard_id: str
    policy_id: str
    title: str
    status: Literal["draft"]


def add_standard_to_draft(
    *,
    actor: tuple[str, str],
    policy_id: str,
    title: str,
    fields: Mapping[str, object],
    graph: GraphHandle,
    access_role_store: AccessRoleStore,
) -> StandardDraftResult:
    """Add a new Standard under a draft Policy (issue #136, Slice 2).

    Ordering mirrors `update_policy_draft` (Slice 1) exactly: (1)
    `_read_transition_target` (backfill + read + not-found -- reused
    unchanged; ownership/status are read directly off the parent Policy
    itself, since a Standard being newly minted has no ownership of its own
    to traverse to yet -- AC-BI-003's "direct" case, distinct from the
    transitive traversal Slices 3-5 need for an *existing* Standard/
    Control); (2) `_authorize_draft_edit` (owner-or-elevated, then must be
    `"draft"`); (3) the new Standard's id is computed via
    `domain_mapper.identity.standard_id(policy_id, title)`; (4)
    `graph_writer.add_standard_to_policy` mints the node, unconditionally
    `status="draft"` (AC-BI-007's governance-status half), `SUPPORTED_BY`-
    linked to `policy_id`.

    Like `update_policy_draft`, this records no audit_events row (PLAN.md
    §1.1 -- only the Slice 6 supersede-fork path does that); it still gets
    the full `_run_mcp_action` started/succeeded/failed log triad for free
    at the MCP layer (AC-BI-014).

    Args:
        actor: The calling caller's verified `(sub, iss)` identity.
        policy_id: The existing draft Policy id to attach the new Standard to.
        title: The new Standard's title.
        fields: Any additional patchable content fields, already restricted
            to `_STANDARD_PATCHABLE_FIELDS` by the MCP-boundary parser
            (`implementation_status`, when present, defaults content-side to
            `"draft"` if omitted -- AC-BI-007's implementation-status half).
        graph: The single-tenant policy graph handle.
        access_role_store: Where `actor`'s active `AccessRole`s are resolved
            from, only ever consulted when `actor` is not the parent
            Policy's owner.

    Returns:
        A `StandardDraftResult` naming the new Standard's id (usable
        immediately in a following call, AC-BI-009), its parent Policy id,
        title, and status.

    Raises:
        PolicyNotFoundError: no `Policy` node exists with `policy_id`.
        PolicyDraftAccessDeniedError: `actor` is neither `policy_id`'s owner
            nor a `SystemOwner`/`SystemAdmin`.
        PolicyInvalidStatusTransitionError: `policy_id`'s Policy is not
            currently `"draft"`.
        PolicyLifecycleGraphUnavailableError: the graph write itself failed
            -- the original `redis.exceptions.RedisError` is chained, never
            leaked to the caller.
    """
    record = _read_transition_target(graph, policy_id)
    context = PolicyLifecycleRuleContext(
        actor=actor,
        owner=(record.owner_subject, record.owner_issuer),
        action="edit",
        current_status=cast("PolicyStatus", record.status),
    )
    _authorize_draft_edit(context, access_role_store=access_role_store)
    new_standard_id = standard_id(policy_id, title)
    try:
        graph_writer.add_standard_to_policy(
            graph,
            policy_id=policy_id,
            standard_id=new_standard_id,
            title=title,
            extra_properties=fields,
        )
    except redis.exceptions.RedisError as exc:
        raise PolicyLifecycleGraphUnavailableError from exc
    return StandardDraftResult(
        standard_id=new_standard_id, policy_id=policy_id, title=title, status="draft"
    )


def _read_standard_with_parent_backfilled(
    graph: GraphHandle, standard_id: str
) -> graph_writer.StandardWithParent:
    """Backfill-then-reread wrapper around `find_standard_with_parent` (issue #136, Slice 3).

    CHANGES.md finding #2 (High): a legitimately-draft Standard minted before
    any status-backfilling call (e.g. via the internal-seed adapter) may
    still carry a `NULL` own `status`. `_read_transition_target`'s own
    backfill-first order (D-7) only covers root-Policy reads that already
    know their own `policy_id` up front; the transitive reads Slices 3-5
    introduce don't know the root `policy_id` until AFTER the first
    traversal, so this reads twice: once to learn `row.policy_id`, then
    `graph_writer.backfill_governance_status` against that id, then a second
    read so the draft-status gate that follows sees the now-backfilled
    value -- never a stale, spuriously-`NULL` one that would cause a false
    `PolicyInvalidStatusTransitionError` rejection.

    Args:
        graph: The single-tenant policy graph handle.
        standard_id: The Standard id to read, plus its parent Policy's
            owner/status.

    Returns:
        The freshly-reread `StandardWithParent` row, post-backfill.

    Raises:
        PolicyStandardNotFoundError: no `Policy -[:SUPPORTED_BY]-> Standard`
            path exists for `standard_id`, on either read.
    """
    row = graph_writer.find_standard_with_parent(graph, standard_id)
    if row is None:
        raise PolicyStandardNotFoundError(standard_id)
    graph_writer.backfill_governance_status(graph, row.policy_id)
    # Node cannot vanish between these two reads in this service's own
    # single-request flow.
    refreshed = graph_writer.find_standard_with_parent(graph, standard_id)
    if refreshed is None:  # pragma: no cover
        raise PolicyStandardNotFoundError(standard_id)
    return refreshed


def update_standard_draft(
    *,
    actor: tuple[str, str],
    standard_id: str,
    fields: Mapping[str, object],
    graph: GraphHandle,
    access_role_store: AccessRoleStore,
) -> StandardDraftResult:
    """PATCH a subset of a draft Standard's own content fields (issue #136, Slice 3).

    Ordering: (1) `_read_standard_with_parent_backfilled` (backfill + one-hop
    transitive read + not-found, CHANGES.md finding #2); (2)
    `_authorize_draft_edit`, built from the PARENT Policy's owner pair (AC-
    BI-003 -- Standard has no ownership field of its own, TASK.md's
    Implementation-decisions paragraph) and the STANDARD's own status (the
    node directly being mutated, not the parent Policy's -- mirrors
    `add_standard_to_draft`'s own "gate reads the existing node being acted
    on" discipline, just one level deeper); (3)
    `graph_writer.update_standard_fields` applies exactly the supplied
    `fields`, nothing else (AC-BI-008).

    Like `update_policy_draft`/`add_standard_to_draft`, this records no
    audit_events row at all (PLAN.md §1.1 -- only the Slice 6 supersede-fork
    path does that); it still gets the full `_run_mcp_action`
    started/succeeded/failed log triad for free at the MCP layer (AC-BI-014).

    Args:
        actor: The calling caller's verified `(sub, iss)` identity.
        standard_id: The draft Standard id to patch.
        fields: Field name -> new value (or `None` to clear), already
            restricted to `_STANDARD_PATCHABLE_FIELDS` by the MCP-boundary
            parser -- this function does not re-validate keys.
        graph: The single-tenant policy graph handle.
        access_role_store: Where `actor`'s active `AccessRole`s are resolved
            from, only ever consulted when `actor` is not the parent
            Policy's owner.

    Returns:
        A `StandardDraftResult` naming the Standard id, its parent Policy id,
        its title, and its (pre-patch, always `"draft"` -- the gate already
        required it) status.

    Raises:
        PolicyStandardNotFoundError: no `Policy -[:SUPPORTED_BY]-> Standard`
            path exists for `standard_id`.
        PolicyDraftAccessDeniedError: `actor` is neither the parent Policy's
            owner nor a `SystemOwner`/`SystemAdmin`.
        PolicyInvalidStatusTransitionError: `standard_id`'s Standard is not
            currently `"draft"`.
        PolicyLifecycleGraphUnavailableError: the graph write itself failed
            -- the original `redis.exceptions.RedisError` is chained, never
            leaked to the caller.
    """
    row = _read_standard_with_parent_backfilled(graph, standard_id)
    context = PolicyLifecycleRuleContext(
        actor=actor,
        owner=(row.policy_owner_subject, row.policy_owner_issuer),
        action="edit",
        current_status=cast("PolicyStatus", row.standard_status),
    )
    _authorize_draft_edit(context, access_role_store=access_role_store)
    try:
        graph_writer.update_standard_fields(graph, standard_id=standard_id, properties=fields)
    except redis.exceptions.RedisError as exc:
        raise PolicyLifecycleGraphUnavailableError from exc
    return StandardDraftResult(
        standard_id=standard_id, policy_id=row.policy_id, title=row.standard_title, status="draft"
    )


@dataclass(frozen=True, slots=True)
class ControlDraftResult:
    """A successful `add_control_to_draft` call's resulting state (issue #136, Slice 4)."""

    control_id: str
    standard_id: str
    policy_id: str
    title: str
    status: Literal["draft"]


def add_control_to_draft(
    *,
    actor: tuple[str, str],
    standard_id: str,
    title: str,
    control_type: Literal["automated", "manual"] = "manual",
    fields: Mapping[str, object],
    graph: GraphHandle,
    access_role_store: AccessRoleStore,
) -> ControlDraftResult:
    """Add a new Control under a draft Standard (issue #136, Slice 4).

    Ordering mirrors `update_standard_draft` (Slice 3) exactly, one level
    deeper: (1) `_read_standard_with_parent_backfilled` (backfill + one-hop
    transitive read + not-found, CHANGES.md finding #2) -- PLAN.md §1.4's own
    correction: ownership for a new Control is derived from the SAME one-hop
    `Policy -[:SUPPORTED_BY]-> Standard` traversal `update-standard-draft`
    already uses (the caller supplies a `standard_id`, exactly like that
    tool), never a two-hop `...->Control` traversal -- that shape belongs to
    `update-control-draft` (Slice 5) alone, which is keyed on an existing
    Control's own id; (2) `_authorize_draft_edit`, built from the PARENT
    Policy's owner pair and the STANDARD's own status (the node directly
    parented, the same "gate reads the existing node being acted on"
    discipline `add_standard_to_draft` established); (3) the new Control's id
    is computed via `domain_mapper.identity.control_id(standard_id, title)`;
    (4) `graph_writer.add_control_to_standard` mints the node, unconditionally
    `status="draft"` (AC-BI-007's governance-status half), `IMPLEMENTED_BY`-
    linked to `standard_id`, `implementation_status` defaulting to
    `"planned"` (NOT `"draft"` -- the one deliberate divergence from
    `add_standard_to_draft`'s own default, per `ps-domain-concepts.md`'s
    "earliest state in status workflow" convention for Control).

    Like every other tool in this issue bar the supersede fork, this records
    no audit_events row (PLAN.md §1.1); it still gets the full
    `_run_mcp_action` started/succeeded/failed log triad for free at the MCP
    layer (AC-BI-014).

    Args:
        actor: The calling caller's verified `(sub, iss)` identity.
        standard_id: The existing draft Standard id to attach the new
            Control to.
        title: The new Control's title.
        control_type: The new Control's `type` (`"automated"` or
            `"manual"`), already validated at the MCP boundary.
        fields: Any additional patchable content fields, already restricted
            to `_CONTROL_PATCHABLE_FIELDS - {"type"}` by the MCP-boundary
            parser (CHANGES.md finding #8 -- `type` is set only via
            `control_type`; `implementation_status`, when present, defaults
            content-side to `"planned"` if omitted -- AC-BI-007's
            implementation-status half).
        graph: The single-tenant policy graph handle.
        access_role_store: Where `actor`'s active `AccessRole`s are resolved
            from, only ever consulted when `actor` is not the parent
            Policy's owner.

    Returns:
        A `ControlDraftResult` naming the new Control's id (usable
        immediately in a following call), its parent Standard and Policy
        ids, title, and status.

    Raises:
        PolicyStandardNotFoundError: no `Policy -[:SUPPORTED_BY]-> Standard`
            path exists for `standard_id`.
        PolicyDraftAccessDeniedError: `actor` is neither the parent Policy's
            owner nor a `SystemOwner`/`SystemAdmin`.
        PolicyInvalidStatusTransitionError: `standard_id`'s Standard is not
            currently `"draft"`.
        PolicyLifecycleGraphUnavailableError: the graph write itself failed
            -- the original `redis.exceptions.RedisError` is chained, never
            leaked to the caller.
    """
    row = _read_standard_with_parent_backfilled(graph, standard_id)
    context = PolicyLifecycleRuleContext(
        actor=actor,
        owner=(row.policy_owner_subject, row.policy_owner_issuer),
        action="edit",
        current_status=cast("PolicyStatus", row.standard_status),
    )
    _authorize_draft_edit(context, access_role_store=access_role_store)
    new_control_id = control_id(standard_id, title)
    try:
        graph_writer.add_control_to_standard(
            graph,
            standard_id=standard_id,
            control_id=new_control_id,
            title=title,
            control_type=control_type,
            extra_properties=fields,
        )
    except redis.exceptions.RedisError as exc:
        raise PolicyLifecycleGraphUnavailableError from exc
    return ControlDraftResult(
        control_id=new_control_id,
        standard_id=standard_id,
        policy_id=row.policy_id,
        title=title,
        status="draft",
    )


def _read_control_with_parent_backfilled(
    graph: GraphHandle, control_id: str
) -> graph_writer.ControlWithParent:
    """Backfill-then-reread wrapper around `find_control_with_parent` (issue #136, Slice 5).

    CHANGES.md finding #2 (High), two-hop case: same fix as
    `_read_standard_with_parent_backfilled` (Slice 3), except the root
    `policy_id` needed to call `backfill_governance_status` is only known
    after a TWO-hop traversal here, not one. A legacy Control minted before
    any status-backfilling call (e.g. via the internal-seed adapter) may
    still carry a `NULL` own `status` even though its root Policy is
    genuinely `draft` -- without this wrapper, `update_control_draft`'s
    draft-status gate would see that stale `NULL` and spuriously reject the
    call with `PolicyInvalidStatusTransitionError`.

    Args:
        graph: The single-tenant policy graph handle.
        control_id: The Control id to read, plus its parent Standard id and
            root Policy's owner/status.

    Returns:
        The freshly-reread `ControlWithParent` row, post-backfill.

    Raises:
        PolicyControlNotFoundError: no `Policy -[:SUPPORTED_BY]-> Standard
            -[:IMPLEMENTED_BY]-> Control` path exists for `control_id`, on
            either read.
    """
    row = graph_writer.find_control_with_parent(graph, control_id)
    if row is None:
        raise PolicyControlNotFoundError(control_id)
    graph_writer.backfill_governance_status(graph, row.policy_id)
    # Node cannot vanish between these two reads in this service's own
    # single-request flow.
    refreshed = graph_writer.find_control_with_parent(graph, control_id)
    if refreshed is None:  # pragma: no cover
        raise PolicyControlNotFoundError(control_id)
    return refreshed


def update_control_draft(
    *,
    actor: tuple[str, str],
    control_id: str,
    fields: Mapping[str, object],
    graph: GraphHandle,
    access_role_store: AccessRoleStore,
) -> ControlDraftResult:
    """PATCH a subset of a draft Control's own content fields (issue #136, Slice 5).

    Ordering: (1) `_read_control_with_parent_backfilled` (backfill + TWO-hop
    transitive read + not-found, CHANGES.md finding #2 -- the genuinely
    two-hop case in this issue, `Policy -[:SUPPORTED_BY]-> Standard
    -[:IMPLEMENTED_BY]-> Control`, distinct from `update_standard_draft`'s/
    `add_control_to_draft`'s own one-hop traversal); (2)
    `_authorize_draft_edit`, built from the ROOT Policy's owner pair (AC-
    BI-003 -- neither Control nor its parent Standard has an ownership field
    of its own, TASK.md's Implementation-decisions paragraph) and the
    CONTROL's own status (the node directly being mutated, not the parent
    Standard's or root Policy's -- mirrors `update_standard_draft`'s own
    "gate reads the existing node being acted on" discipline, one hop
    deeper); (3) `graph_writer.update_control_fields` applies exactly the
    supplied `fields`, nothing else (AC-BI-008).

    Like every PATCH/add tool in this issue bar the supersede fork, this
    records no audit_events row at all (PLAN.md §1.1 -- only Slice 6's
    supersede-fork path does that); it still gets the full `_run_mcp_action`
    started/succeeded/failed log triad for free at the MCP layer (AC-BI-014).

    Args:
        actor: The calling caller's verified `(sub, iss)` identity.
        control_id: The draft Control id to patch.
        fields: Field name -> new value (or `None` to clear), already
            restricted to `_CONTROL_PATCHABLE_FIELDS` by the MCP-boundary
            parser -- this function does not re-validate keys.
        graph: The single-tenant policy graph handle.
        access_role_store: Where `actor`'s active `AccessRole`s are resolved
            from, only ever consulted when `actor` is not the root Policy's
            owner.

    Returns:
        A `ControlDraftResult` naming the Control id, its parent Standard and
        root Policy ids, its title, and its (pre-patch, always `"draft"` --
        the gate already required it) status.

    Raises:
        PolicyControlNotFoundError: no `Policy -[:SUPPORTED_BY]-> Standard
            -[:IMPLEMENTED_BY]-> Control` path exists for `control_id`.
        PolicyDraftAccessDeniedError: `actor` is neither the root Policy's
            owner nor a `SystemOwner`/`SystemAdmin`.
        PolicyInvalidStatusTransitionError: `control_id`'s Control is not
            currently `"draft"`.
        PolicyLifecycleGraphUnavailableError: the graph write itself failed
            -- the original `redis.exceptions.RedisError` is chained, never
            leaked to the caller.
    """
    row = _read_control_with_parent_backfilled(graph, control_id)
    context = PolicyLifecycleRuleContext(
        actor=actor,
        owner=(row.policy_owner_subject, row.policy_owner_issuer),
        action="edit",
        current_status=cast("PolicyStatus", row.control_status),
    )
    _authorize_draft_edit(context, access_role_store=access_role_store)
    try:
        graph_writer.update_control_fields(graph, control_id=control_id, properties=fields)
    except redis.exceptions.RedisError as exc:
        raise PolicyLifecycleGraphUnavailableError from exc
    return ControlDraftResult(
        control_id=control_id,
        standard_id=row.standard_id,
        policy_id=row.policy_id,
        title=row.control_title,
        status="draft",
    )
