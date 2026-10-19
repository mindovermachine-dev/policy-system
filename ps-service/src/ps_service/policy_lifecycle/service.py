"""Policy lifecycle service: create, read, edit and move Policies through governance.

Each public function is the single delegate of one MCP tool. The tool layer only parses input
and maps errors; the business rules, ordering and audit trail live here. Every function takes
the verified caller identity as a `(sub, iss)` tuple together with the collaborators it needs
(graph handle, audit store, access-role store, Graph Write Gateway submitter), so it opens no
connection of its own.

Lifecycle: a Policy is created as a `draft` (optionally forked from an `approved` Policy as a
successor), edited while `draft`, proposed (`draft` to `proposed`), then approved (`proposed` to
`approved`) or rejected (`proposed` to `draft`); the owner may also revert a `proposed` Policy to
`draft`. A status change cascades to every Standard and Control in the Policy's tree. Approving a
successor auto-deprecates the `approved` Policy it supersedes.

Ordering of every audited action: read the current state, evaluate the gates in order, and on the
first failing gate record a `rejected` audit event and raise; when all gates pass record an
`applied` audit event BEFORE the graph write, write the graph through the Graph Write Gateway as
one group linked to that event, and if the write fails record a `failed` event for the same
action and resource and raise `PolicyLifecycleGraphUnavailableError`. Content edits of a draft
(`update_*`, `add_*`) write no audit event.

Importing `ps_service.policy_lifecycle.audit_actions` here registers the `policy.*` audit
actions; the component that emits those events must import that module before any event is
recorded, and this module is that component.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import redis.exceptions

import ps_service.policy_lifecycle.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers this component's own audit actions before any lifecycle call can reach AuditStore.record
from ps_service.audit.store import EventIdAuditStore
from ps_service.authz.models import AccessRole
from ps_service.authz.service import resolve_active_roles
from ps_service.domain_mapper.identity import control_id, standard_id
from ps_service.domain_mapper.identity import policy_id as compute_policy_id
from ps_service.domain_schema import enum_values, patchable_fields
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
from ps_service.policy_lifecycle.graph_submission import (
    GraphWriteTarget,
    single_tenant_graph_name,
)
from ps_service.policy_lifecycle.rules import (
    PolicyLifecycleRuleContext,
    block_self_approval,
    require_owner,
    require_status,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from typing import Literal

    from ps_service.audit.store import AuditStore
    from ps_service.authz.store import AccessRoleStore
    from ps_service.policy_lifecycle.graph_submission import LifecycleSubmitter
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

# `AccessRole`s that see and edit a draft Policy regardless of ownership.
_DRAFT_VISIBILITY_OVERRIDE_ROLES = frozenset({AccessRole.SYSTEM_OWNER, AccessRole.SYSTEM_ADMIN})

# `update-policy-draft`'s patchable-field allow-list, derived from the domain schema's property
# flags: every Policy property not flagged identity-bearing (`title`, `owner_id`,
# `owner_subject`, `owner_issuer`) or lifecycle-managed (`status`, `version`). Declared once here
# and imported into `mcp_interface.mcp_server` (its `_parse_patch_fields` call site) so the two
# layers cannot drift.
_POLICY_PATCHABLE_FIELDS = patchable_fields("Policy")

# `add-standard-to-draft`'s and `update-standard-draft`'s patchable-field allow-list, derived
# from the domain schema's property flags. Excludes `title` (identity-bearing: `standard_id` is
# content-derived from it), `status` and `version` (lifecycle-managed, never set through the
# content tools). `implementation_status` is included: it is the Standard's own workflow field,
# distinct from governance `status`, and must be independently settable.
_STANDARD_PATCHABLE_FIELDS = patchable_fields("Standard")
_STANDARD_IMPLEMENTATION_STATUS_VALUES = enum_values("Standard", "implementation_status")

# `add-control-to-draft`'s and `update-control-draft`'s patchable-field allow-list, derived from
# the domain schema's property flags. Excludes `title` (identity-bearing: `control_id` is
# content-derived from it) and `status` (governance status, lifecycle-managed only). Unlike
# `_STANDARD_PATCHABLE_FIELDS` it includes `"type"`, because `update-control-draft` is the only
# post-creation path to change it. This is the full set; `add-control-to-draft` narrows it by
# `"type"` at the MCP boundary (`mcp_interface.mcp_server`'s
# `_ADD_CONTROL_TO_DRAFT_PATCHABLE_FIELDS`), since creation sets the type through `control_type`.
_CONTROL_PATCHABLE_FIELDS = patchable_fields("Control")
_CONTROL_IMPLEMENTATION_STATUS_VALUES = enum_values("Control", "implementation_status")

_CREATE_DRAFT_ACTION = "policy.create_draft"
_PROPOSE_ACTION = "policy.propose"
_APPROVE_ACTION = "policy.approve"
_REJECT_ACTION = "policy.reject"
_REVERT_ACTION = "policy.revert"
_AUTO_DEPRECATE_ACTION = "policy.auto_deprecate"
_POLICY_RESOURCE_TYPE = "policy"


@dataclass(frozen=True, slots=True)
class ControlDraftInput:
    """One Control to mint under a Standard when a draft Policy is created.

    Attributes:
        title: The Control's title; its id is derived from the parent Standard id and this title.
        control_type: The Control's `type`, `"automated"` or `"manual"` (default `"manual"`).
    """

    title: str
    control_type: Literal["automated", "manual"] = "manual"


@dataclass(frozen=True, slots=True)
class StandardDraftInput:
    """One Standard, with its optional Controls, to mint when a draft Policy is created.

    Attributes:
        title: The Standard's title; its id is derived from the parent Policy id and this title.
        controls: The Controls to mint under this Standard; empty when it starts with none.
    """

    title: str
    controls: tuple[ControlDraftInput, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PolicyDraftResult:
    """Identity and initial state of a draft Policy that `create_policy_draft` created.

    Attributes:
        policy_id: The new Policy's content-derived id.
        title: The Policy's title.
        status: Always `"draft"`.
        version: `"1"` for an ordinary draft; for a successor fork, the prior version plus one,
            as a decimal string.
        owner_subject: The creator's subject claim; the Policy's owner.
        owner_issuer: The creator's issuer claim; the Policy's owner.
        standard_ids: Ids of every Standard minted (or forked) under the Policy.
        control_ids: Ids of every Control minted (or forked) under those Standards.
        superseded_policy_id: The `approved` Policy this draft was forked from, or `None` for an
            ordinary draft.
        capability_ids: The Capabilities the new draft governs; empty for a fork or when none were
            claimed.
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
    """Compute the id of every requested Standard and Control without any graph I/O.

    The ids are fixed before the `applied` audit event is written so that the event's
    `affected_node_ids` and the graph write name exactly the same nodes.

    Args:
        new_policy_id: Id of the Policy the Standards will belong to.
        standards: The requested Standards and their Controls; may be empty.

    Returns:
        One `StandardDraft` per input Standard, each with its derived id and Controls.
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


# Property keys never carried over as a forked node's `extra_properties`: `id` and `status` are
# set by the fork itself (a new node's id differs from its source's, and a forked node is always
# a `"draft"` although its source is `"approved"`), and `title` is carried separately on each
# forked record. `type` is also stripped from a forked Control's extra properties because
# `graph_writer.create_policy_draft` always sets it from `ControlDraft.control_type`.
_FORKED_STANDARD_STRIPPED_KEYS = frozenset({"id", "title", "status"})
_FORKED_CONTROL_STRIPPED_KEYS = frozenset({"id", "title", "status", "type"})


def _build_forked_standard_drafts(
    new_policy_id: str, prior_policy_id: str, graph: GraphHandle
) -> tuple[graph_writer.StandardDraft, ...]:
    """Copy `prior_policy_id`'s Standard and Control tree as new drafts under `new_policy_id`.

    Reads the prior tree's full content and gives every copied Standard and Control a new id derived
    from `new_policy_id`, so the copies are independent nodes. Properties `id`, `title` and `status`
    (and `type` for a Control) are not copied as extra properties because the id, title and
    `"draft"` status are set explicitly. The ids are computed before the `applied` audit event so
    the event and the graph write name the same nodes. Read-only against the prior tree.

    Args:
        new_policy_id: Id of the successor Policy that will own the copies.
        prior_policy_id: Id of the Policy whose tree is copied.
        graph: The single-tenant policy graph handle.

    Returns:
        One `StandardDraft` per Standard of the prior tree, each with its Controls; empty when the
        prior has no Standards.

    Raises:
        redis.exceptions.RedisError: the graph read failed (propagated unwrapped).
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
    """Reject a fresh draft's claim on Capabilities that do not exist or already have a governor.

    Reads each Capability's current governing Policy. When any claimed Capability is missing or
    already governed, records a `rejected` `policy.create_draft` audit event (`capability_ids`
    empty, because nothing is claimed) and then raises; no graph write is attempted.

    Args:
        graph: The single-tenant policy graph handle.
        audit_store: Where the `rejected` audit event is recorded.
        actor: The creating caller's `(sub, iss)` identity, recorded on the event.
        new_policy_id: Id of the draft being created, recorded as the event's resource id.
        capability_ids: The claimed Capability ids; expected non-empty and de-duplicated.

    Raises:
        PolicyCapabilityNotFoundError: at least one claimed Capability does not exist (checked
            first, so it wins over the governed case).
        PolicyCapabilityAlreadyGovernedError: every claimed Capability exists but at least one
            already has a governing Policy.
        redis.exceptions.RedisError: the graph read failed (propagated unwrapped).
        AuditPostgresUnavailableError: the audit database is unreachable (propagated).
        AuditPersistenceError: the audit insert failed (propagated).
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
    """Record one standalone `policy.create_draft` audit event.

    Args:
        audit_store: Where the event is recorded.
        actor: The creating caller's `(sub, iss)` identity.
        resource_id: Id of the Policy the event is about.
        outcome: `"applied"`, `"rejected"` or `"failed"`.
        details: The event's `details` payload, validated against the registered action model.

    Raises:
        AuditUnknownActionError: the action has no registered details model (wiring error).
        AuditInvalidDetailsError: `details` does not match the action's model.
        AuditPostgresUnavailableError: the audit database is unreachable.
        AuditPersistenceError: the audit insert failed.
    """
    audit_store.record_standalone(
        actor_subject=actor[0],
        actor_issuer=actor[1],
        action=_CREATE_DRAFT_ACTION,
        resource_type=_POLICY_RESOURCE_TYPE,
        resource_id=resource_id,
        outcome=outcome,
        details=details,
    )


def _record_applied_with_event_id(
    audit_store: AuditStore,
    *,
    actor: tuple[str, str],
    action: str,
    resource_id: str,
    details: Mapping[str, object],
) -> str:
    """Record an `applied` `policy.*` audit event and return the id of the row written.

    The row is written before any graph write, and the returned id lets the gateway group name the
    audit row it belongs to. An audit write failure propagates unchanged.

    Args:
        audit_store: Where the event is recorded; must be able to report the id of the row it wrote.
        actor: The caller's `(sub, iss)` identity.
        action: The registered `policy.*` audit action name.
        resource_id: Id of the Policy the event is about.
        details: The event's `details` payload, validated against the action's model.

    Returns:
        The id of the `applied` audit row.

    Raises:
        TypeError: `audit_store` cannot report the id of the row it wrote (a wiring error).
        AuditUnknownActionError: `action` has no registered details model.
        AuditInvalidDetailsError: `details` does not match the action's model.
        AuditPostgresUnavailableError: the audit database is unreachable.
        AuditPersistenceError: the audit insert failed.
    """
    if not isinstance(audit_store, EventIdAuditStore):
        message = f"{type(audit_store).__name__} cannot report an audit event id"
        raise TypeError(message)
    return audit_store.record_standalone_with_event_id(
        actor_subject=actor[0],
        actor_issuer=actor[1],
        action=action,
        resource_type=_POLICY_RESOURCE_TYPE,
        resource_id=resource_id,
        outcome="applied",
        details=details,
    )


@dataclass(frozen=True, slots=True)
class _NewPolicy:
    """The Policy `_write_new_policy` writes: identity, content and audit details.

    Attributes:
        policy_id: Id of the new Policy.
        title: The Policy's title.
        standard_drafts: Standards (with Controls) to mint under it.
        supersedes_policy_id: Id of the `approved` Policy being forked, or `None`.
        version: The Policy's version string.
        capability_ids: Capabilities claimed at creation; empty for a fork or no claim.
        details: The `details` payload shared by the `applied` and `failed` audit events.
    """

    policy_id: str
    title: str
    standard_drafts: tuple[graph_writer.StandardDraft, ...]
    supersedes_policy_id: str | None
    version: str
    capability_ids: tuple[str, ...]
    details: Mapping[str, object]


def _write_new_policy(
    *,
    graph: GraphHandle,
    audit_store: AuditStore,
    submitter: LifecycleSubmitter,
    actor: tuple[str, str],
    new: _NewPolicy,
) -> bool:
    """Record `applied`, write the new Policy, and record `failed` if the write fails.

    The `applied` row is written before the graph write and its id anchors the one gateway group.
    A Policy that claims Capabilities is written as a guarded group that fails if a claimed
    Capability gained a governor since it was read.

    Args:
        graph: The single-tenant policy graph handle.
        audit_store: Where the `applied` and any `failed` events are recorded.
        submitter: The Graph Write Gateway the group is submitted through.
        actor: The creating caller's `(sub, iss)` identity; becomes the owner.
        new: The Policy to write.

    Returns:
        `True` when the Policy was written; `False` only when a claimed Capability was governed
        by another Policy at write time, in which case nothing was written.

    Raises:
        PolicyLifecycleGraphUnavailableError: the write failed (the message starts with
            `FalkorDB write failed: ` for a gateway failure); a `failed` event with the same
            `details` was recorded first. A driver `RedisError` is chained as the cause.
        AuditPostgresUnavailableError: an audit event could not be written (propagated).
        AuditPersistenceError: an audit insert failed (propagated).
        TypeError: `audit_store` cannot report the id of the row it wrote (a wiring error).
    """
    try:
        audit_event_id = _record_applied_with_event_id(
            audit_store,
            actor=actor,
            action=_CREATE_DRAFT_ACTION,
            resource_id=new.policy_id,
            details=new.details,
        )
        target = GraphWriteTarget(submitter, single_tenant_graph_name())
        if new.capability_ids:
            return graph_writer.create_claimed_policy_draft(
                graph,
                target,
                policy_id=new.policy_id,
                title=new.title,
                owner=actor,
                standards=new.standard_drafts,
                capability_ids=new.capability_ids,
                audit_event_id=audit_event_id,
            )
        graph_writer.create_policy_draft(
            target,
            policy_id=new.policy_id,
            title=new.title,
            owner=actor,
            standards=new.standard_drafts,
            supersedes_policy_id=new.supersedes_policy_id,
            version=new.version,
            audit_event_id=audit_event_id,
        )
    except redis.exceptions.RedisError as exc:
        _record_create_draft(
            audit_store,
            actor=actor,
            resource_id=new.policy_id,
            outcome="failed",
            details=new.details,
        )
        raise PolicyLifecycleGraphUnavailableError from exc
    except PolicyLifecycleGraphUnavailableError:
        _record_create_draft(
            audit_store,
            actor=actor,
            resource_id=new.policy_id,
            outcome="failed",
            details=new.details,
        )
        raise
    return True


def create_policy_draft(
    *,
    actor: tuple[str, str],
    title: str,
    standards: tuple[StandardDraftInput, ...] = (),
    supersedes_policy_id: str | None = None,
    capability_ids: tuple[str, ...] = (),
    graph: GraphHandle,
    audit_store: AuditStore,
    submitter: LifecycleSubmitter,
) -> PolicyDraftResult:
    """Create a draft Policy owned by `actor`, or a successor draft forked from an approved Policy.

    An ordinary call mints a version-1 Policy whose id is derived from `title`, plus any requested
    Standards and Controls, all with status `"draft"`, and optionally claims Capabilities for it.
    When `supersedes_policy_id` is given the call instead forks an amendment: the successor copies
    the prior Policy's current Standard and Control tree as new, independently editable nodes.

    Business Rules:
        - Ordinary draft: id is `policy_id(title)`, version `"1"`. A title that derives an existing
          Policy id is refused.
        - Fork: the prior Policy must exist and be `"approved"`. The successor id is
          `policy_id(title, prior_policy_id)` and its version is the prior version plus one. The
          caller-supplied `standards` and `capability_ids` are silently ignored. One
          `(prior)-[:SUPERSEDED_BY]->(new)` edge links the two Policies; the prior tree is never
          modified. A successor takes over the prior's Capabilities only when it is approved.
        - Capability claims on an ordinary draft: each id must name an existing Capability that no
          Policy governs; duplicates are dropped, keeping first-seen order.
        - A rejection of the fork precondition (missing or not approved prior) records no audit
          event; a title collision or a bad Capability claim records a `rejected` event.

    Args:
        actor: The creating caller's verified `(sub, iss)` identity; becomes the Policy's
            `owner_subject` and `owner_issuer`.
        title: The new Policy's title; the id is derived from it (and the prior id for a fork).
        standards: Standards, each with optional Controls, to mint under the Policy, all with
            status `"draft"`. Ignored when `supersedes_policy_id` is given. Empty by default.
        supersedes_policy_id: Id of an existing `"approved"` Policy to fork a successor from, or
            `None` (default) for an ordinary version-1 draft.
        capability_ids: Capabilities the fresh draft governs from creation. Ignored when
            `supersedes_policy_id` is given. Empty by default.
        graph: The single-tenant policy graph handle.
        audit_store: Where `policy.create_draft` audit events are recorded as standalone rows.
        submitter: The Graph Write Gateway used for the Policy write and for backfilling a legacy
            prior tree; the backfill only writes when some node of that tree needs it.

    Returns:
        A `PolicyDraftResult` with the new Policy's id, version, owner, and the ids of every
        Standard and Control minted.

    Raises:
        PolicyNotFoundError: `supersedes_policy_id` names no Policy.
        PolicySupersedePriorNotApprovedError: the prior Policy exists but is not `"approved"`.
        PolicyTitleAlreadyExistsError: the derived Policy id already exists; a `rejected` audit
            event is recorded first and nothing is written.
        PolicyCapabilityNotFoundError: a claimed Capability does not exist; a `rejected` event is
            recorded first and nothing is written.
        PolicyCapabilityAlreadyGovernedError: a claimed Capability already has a governing Policy,
            or gained one between the check and the guarded write (then an `applied` event is
            followed by a `failed` event and nothing is written).
        PolicyLifecycleGraphUnavailableError: the graph write failed, or a backfill write of the
            prior tree failed; after an `applied` event a `failed` follow-up event is recorded for
            the same action and resource first.
        redis.exceptions.RedisError: a graph read failed (propagated unwrapped).
        AuditPostgresUnavailableError: an audit event could not be written (propagated).
        AuditPersistenceError: an audit insert failed (propagated).

    Side Effects:
        Records `policy.create_draft` audit events (`rejected`, or `applied` then optionally
        `failed`). On success writes the Policy, its Standards and Controls (and the
        `SUPERSEDED_BY` edge or the `GOVERNED_BY` claims) as one Graph Write Gateway group linked to
        the `applied` event. On a fork, may also backfill missing `version`/`status` properties on
        the prior tree through the gateway.
    """
    owner_subject, owner_issuer = actor

    new_version = "1"
    if supersedes_policy_id is not None:
        prior_record = _read_transition_target(
            graph, supersedes_policy_id, submitter_source=_eager(submitter)
        )
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

    details: Mapping[str, object] = {
        "affected_node_ids": affected_node_ids,
        "to_status": "draft",
        "supersedes_policy_id": supersedes_policy_id,
        "capability_ids": claimed_capability_ids,
    }
    created = _write_new_policy(
        graph=graph,
        audit_store=audit_store,
        submitter=submitter,
        actor=actor,
        new=_NewPolicy(
            policy_id=new_policy_id,
            title=title,
            standard_drafts=standard_drafts,
            supersedes_policy_id=supersedes_policy_id,
            version=new_version,
            capability_ids=claimed_capability_ids,
            details=details,
        ),
    )

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
    """One Control in the tree returned by `get_policy`.

    Attributes:
        control_id: The Control's id.
        title: The Control's title.
        control_type: The Control's `type`, for example `"automated"` or `"manual"`.
        status: The Control's governance status.
    """

    control_id: str
    title: str
    control_type: str
    status: str


@dataclass(frozen=True, slots=True)
class StandardView:
    """One Standard, with its Controls, in the tree returned by `get_policy`.

    Attributes:
        standard_id: The Standard's id.
        title: The Standard's title.
        status: The Standard's governance status.
        controls: The Standard's Controls; empty when it has none.
    """

    standard_id: str
    title: str
    status: str
    controls: tuple[ControlView, ...]


@dataclass(frozen=True, slots=True)
class PolicyView:
    """A Policy's own fields plus its full Standard and Control tree, as returned by `get_policy`.

    Attributes:
        policy_id: The Policy's id.
        title: The Policy's title.
        status: The Policy's governance status: `draft`, `proposed`, `approved` or `deprecated`.
        version: The Policy's version, a decimal string.
        owner_subject: The owner's subject claim.
        owner_issuer: The owner's issuer claim.
        standards: The Policy's Standards; empty when it has none.
    """

    policy_id: str
    title: str
    status: PolicyStatus
    version: str
    owner_subject: str
    owner_issuer: str
    standards: tuple[StandardView, ...]


def _to_policy_view(record: graph_writer.PolicyRecord) -> PolicyView:
    """Map the graph writer's raw `PolicyRecord` onto the public `PolicyView` shape.

    The two shapes are kept separate on purpose: the record is an internal grouping of graph rows,
    the view is the contract of `get_policy`, and coupling them would make a change to one silently
    change the other.

    Args:
        record: A Policy tree as read from the graph.

    Returns:
        The equivalent `PolicyView`, with Standards and Controls in record order.
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
    policy_id: str,
    graph: GraphHandle,
    submitter_source: Callable[[], LifecycleSubmitter],
) -> PolicyView:
    """Read a Policy and its full Standard and Control tree.

    A Policy minted before `version` and the Standard/Control `status` properties existed is
    backfilled first, so the read never sees a missing status.

    Business Rules:
        - A Policy is readable in every status, `draft` included. Authentication is checked by the
          caller; this function applies no per-Policy visibility rule.
        - Reads write no audit event.

    Args:
        policy_id: Id of the Policy to read.
        graph: The single-tenant policy graph handle.
        submitter_source: Returns the Graph Write Gateway for the backfill; called only when some
            node of the tree needs a fill, so reading an already backfilled Policy needs no gateway.

    Returns:
        A `PolicyView` of the Policy and its tree.

    Raises:
        PolicyNotFoundError: no Policy has `policy_id`.
        PolicyLifecycleGraphUnavailableError: a needed backfill write failed
            (`FalkorDB write failed: ...`).
        redis.exceptions.RedisError: a graph read failed (propagated unwrapped).

    Side Effects:
        May backfill missing `version` and `status` properties through the gateway. Otherwise
        read-only.
    """
    graph_writer.backfill_governance_status(graph, policy_id, submitter_source=submitter_source)
    record = graph_writer.read_policy_tree(graph, policy_id)
    if record is None:
        raise PolicyNotFoundError(policy_id)
    return _to_policy_view(record)


@dataclass(frozen=True, slots=True)
class PolicyProposeResult:
    """Resulting state of a successful `propose_policy` call.

    Attributes:
        policy_id: The proposed Policy's id.
        status: Always `"proposed"`.
        standard_ids: Ids of every Standard in the Policy's tree.
        control_ids: Ids of every Control in the Policy's tree.
    """

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
    submitter: LifecycleSubmitter,
) -> PolicyProposeResult:
    """Propose a draft Policy for approval, moving its whole tree to `status="proposed"`.

    Gates are checked in this order; the first failure records a `rejected` audit event (with the
    failed gate's `reason_code`) and raises: the caller owns the Policy (`access_denied`), the
    Policy is `"draft"` (`invalid_status`), and the Policy has at least one Standard
    (`incomplete_for_proposal`; a Standard without Controls is allowed). When all pass, an `applied`
    event is recorded before the graph write.

    Args:
        actor: The proposing caller's verified `(sub, iss)` identity; must be the Policy's owner.
        policy_id: Id of the Policy to propose.
        graph: The single-tenant policy graph handle.
        audit_store: Where `policy.propose` audit events are recorded as standalone rows.
        submitter: The Graph Write Gateway used for the status cascade and for backfilling a
            legacy tree; the backfill only writes when some node needs it.

    Returns:
        A `PolicyProposeResult` with the Policy's id, the new status and the ids of its tree.

    Raises:
        PolicyNotFoundError: no Policy has `policy_id`.
        PolicyDraftAccessDeniedError: `actor` is not the Policy's owner.
        PolicyInvalidStatusTransitionError: the Policy is not currently `"draft"`.
        PolicyIncompleteForProposalError: the Policy has no Standards.
        PolicyLifecycleGraphUnavailableError: the graph write failed; a `failed` follow-up event
            for the same action and resource is recorded first. A failed backfill raises it too.
        redis.exceptions.RedisError: a graph read failed (propagated unwrapped).
        AuditPostgresUnavailableError: an audit event could not be written (propagated).
        AuditPersistenceError: an audit insert failed (propagated).

    Side Effects:
        Records `policy.propose` audit events (`rejected`, or `applied` then optionally `failed`).
        On success sets `status` to `"proposed"` on the Policy and every Standard and Control as one
        gateway group linked to the `applied` event. May backfill missing `version` and `status`.
    """
    record = _read_transition_target(graph, policy_id, submitter_source=_eager(submitter))

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
        submitter=submitter,
    )

    return PolicyProposeResult(
        policy_id=policy_id,
        status="proposed",
        standard_ids=standard_ids,
        control_ids=control_ids,
    )


def _tree_node_ids(record: graph_writer.PolicyRecord) -> tuple[str, ...]:
    """List a Policy tree's node ids: the Policy id, then every Standard id, then every Control id.

    Args:
        record: A Policy tree as read from the graph.

    Returns:
        The ids in that order; the Policy id is always first.
    """
    standard_ids = tuple(standard.id for standard in record.standards)
    control_ids = tuple(
        control.id for standard in record.standards for control in standard.controls
    )
    return (record.id, *standard_ids, *control_ids)


@dataclass(frozen=True, slots=True)
class _Repoint:
    """The move of governed Capabilities when a fork is approved: from the prior Policy to the fork.

    Attributes:
        prior_id: Id of the superseded Policy that currently governs the Capabilities.
        capability_ids: The Capabilities to move to the fork.
    """

    prior_id: str
    capability_ids: tuple[str, ...]


def _write_cascade(
    graph: GraphHandle,
    submitter: LifecycleSubmitter,
    *,
    policy_id: str,
    target_status: str,
    repoint: _Repoint | None,
    audit_event_id: str,
) -> bool:
    """Submit the one status-cascade group, optionally moving governed Capabilities with it.

    Args:
        graph: The single-tenant policy graph handle.
        submitter: The Graph Write Gateway the group is submitted through.
        policy_id: Id of the Policy whose tree gets `target_status`.
        target_status: The status written to the Policy and its Standards and Controls.
        repoint: The Capability move to include, or `None` for a plain cascade.
        audit_event_id: Id of the `applied` audit row the group is linked to.

    Returns:
        `True` when the group was written; `False` only when a Capability move was requested and
        its guard failed (the prior's governed set changed), in which case nothing was written.

    Raises:
        PolicyLifecycleGraphUnavailableError: the gateway write failed
            (`FalkorDB write failed: ...`).
        redis.exceptions.RedisError: a graph read failed (propagated unwrapped).
    """
    target = GraphWriteTarget(submitter, single_tenant_graph_name())
    if repoint is None:
        graph_writer.cascade_status(
            graph,
            target,
            policy_id=policy_id,
            target_status=target_status,
            audit_event_id=audit_event_id,
        )
        return True
    return graph_writer.approve_fork_repoint(
        graph,
        target,
        policy_id=policy_id,
        prior_id=repoint.prior_id,
        capability_ids=repoint.capability_ids,
        target_status=target_status,
        audit_event_id=audit_event_id,
    )


def _cascade_with_audit(
    *,
    actor: tuple[str, str],
    policy_id: str,
    spec: _TransitionSpec,
    affected_node_ids: tuple[str, ...],
    graph: GraphHandle,
    audit_store: AuditStore,
    submitter: LifecycleSubmitter,
    repoint: _Repoint | None = None,
) -> None:
    """Run the audited cascade tail: `applied` event, status cascade, `failed` event on failure.

    The `applied` event is written first and its id links the single gateway group that cascades the
    status over the tree. When `repoint` is given (approving a fork whose prior governs
    Capabilities) the same group also moves the `GOVERNED_BY` edges, guarded so that it is refused
    if the governed set changed; the `applied` event then carries the intended `capability_ids`. A
    guard refusal records a `failed` event (`reason_code="governance_conflict"`, empty
    `capability_ids`, because nothing moved) and raises.

    Args:
        actor: The caller's `(sub, iss)` identity, recorded on the events.
        policy_id: Id of the Policy whose tree is transitioned.
        spec: The audit action name and the from and to statuses.
        affected_node_ids: Ids of every node in the tree, recorded on the events.
        graph: The single-tenant policy graph handle.
        audit_store: Where the events are recorded.
        submitter: The Graph Write Gateway the group is submitted through.
        repoint: The Capability move to include, or `None` (default) for a plain cascade.

    Raises:
        PolicyLifecycleGraphUnavailableError: the write failed after the `applied` event; a `failed`
            follow-up event is recorded first. A gateway failure message starts with
            `FalkorDB write failed: `; a driver `RedisError` is chained as the cause.
        PolicyGovernanceConflictError: `repoint` was given and the governed set no longer matched
            at write time; nothing was written.
        TypeError: `audit_store` cannot report the id of the row it wrote (a wiring error).
        AuditPostgresUnavailableError: an audit event could not be written (propagated).
        AuditPersistenceError: an audit insert failed (propagated).

    Side Effects:
        Records one `applied` and optionally one `failed` audit event; on success writes the status
        (and Capability move) as one gateway group.
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

    def _record_failed(details: dict[str, object]) -> None:
        audit_store.record_standalone(
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            action=spec.action,
            resource_type=_POLICY_RESOURCE_TYPE,
            resource_id=policy_id,
            outcome="failed",
            details=details,
        )

    audit_event_id = _record_applied_with_event_id(
        audit_store,
        actor=actor,
        action=spec.action,
        resource_id=policy_id,
        details=applied_details,
    )
    try:
        moved = _write_cascade(
            graph,
            submitter,
            policy_id=policy_id,
            target_status=spec.target_status,
            repoint=repoint,
            audit_event_id=audit_event_id,
        )
    except redis.exceptions.RedisError as exc:
        _record_failed(base_details)
        raise PolicyLifecycleGraphUnavailableError from exc
    except PolicyLifecycleGraphUnavailableError:
        _record_failed(base_details)
        raise
    if not moved:
        _record_failed({**base_details, "reason_code": "governance_conflict", "capability_ids": ()})
        raise PolicyGovernanceConflictError(policy_id)


def _eager(submitter: LifecycleSubmitter) -> Callable[[], LifecycleSubmitter]:
    """Wrap an already available gateway submitter as a zero-argument source of it.

    Args:
        submitter: The Graph Write Gateway submitter the caller already holds.

    Returns:
        A callable that returns `submitter` and has no other effect.
    """
    return lambda: submitter


def _read_transition_target(
    graph: GraphHandle, policy_id: str, *, submitter_source: Callable[[], LifecycleSubmitter]
) -> graph_writer.PolicyRecord:
    """Backfill, read and return the Policy tree a lifecycle action operates on.

    Runs the governance backfill (which writes through the gateway only when some node lacks
    `version` or `status`) and then reads the tree. Shared by every action that needs the current
    tree; reads that follow a Standard or Control id use the `_read_*_with_parent_backfilled`
    helpers instead.

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: Id of the Policy to read.
        submitter_source: Returns the gateway for the backfill; called only when a fill is needed.

    Returns:
        The Policy's `PolicyRecord`, with `version` and `status` populated.

    Raises:
        PolicyNotFoundError: no Policy has `policy_id`.
        PolicyLifecycleGraphUnavailableError: a needed backfill write failed.
        redis.exceptions.RedisError: a graph read failed (propagated unwrapped).
    """
    graph_writer.backfill_governance_status(graph, policy_id, submitter_source=submitter_source)
    record = graph_writer.read_policy_tree(graph, policy_id)
    if record is None:
        raise PolicyNotFoundError(policy_id)
    return record


@dataclass(frozen=True, slots=True)
class _TransitionGate:
    """One already-evaluated precondition of a status transition.

    The transition function builds its own rule context and picks the error and `reason_code`, since
    those differ per action; `_check_gates` only consumes the results, and the first gate that is
    not allowed decides the rejection.

    Attributes:
        allowed: Whether the precondition holds.
        reason_code: The `reason_code` recorded on the `rejected` audit event when it does not.
        error: The exception raised when it does not hold.
    """

    allowed: bool
    reason_code: _TransitionReasonCode
    error: Exception


@dataclass(frozen=True, slots=True)
class _TransitionSpec:
    """The facts that identify one status transition.

    Attributes:
        action: The registered `policy.*` audit action name.
        from_status: The Policy's status before the transition.
        target_status: The status the transition writes.
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
    submitter: LifecycleSubmitter,
) -> None:
    """Check the gates, then run the audited cascade.

    Args:
        actor: The caller's `(sub, iss)` identity.
        policy_id: Id of the Policy being transitioned.
        spec: The audit action and from and to statuses.
        affected_node_ids: Ids of every node of the tree, recorded on the audit events.
        gates: The preconditions, evaluated in order.
        graph: The single-tenant policy graph handle.
        audit_store: Where the audit events are recorded.
        submitter: The Graph Write Gateway the cascade is submitted through.

    Raises:
        Exception: the error of the first gate that is not allowed, after its `rejected` event
            is recorded.
        PolicyLifecycleGraphUnavailableError: the cascade write failed (after a `failed` event).
        AuditPostgresUnavailableError: an audit event could not be written (propagated).
        AuditPersistenceError: an audit insert failed (propagated).
        TypeError: `audit_store` cannot report the id of the row it wrote (a wiring error).
    """
    _check_gates(actor=actor, policy_id=policy_id, spec=spec, gates=gates, audit_store=audit_store)
    _cascade_with_audit(
        actor=actor,
        policy_id=policy_id,
        spec=spec,
        affected_node_ids=affected_node_ids,
        graph=graph,
        audit_store=audit_store,
        submitter=submitter,
    )


def _check_gates(
    *,
    actor: tuple[str, str],
    policy_id: str,
    spec: _TransitionSpec,
    gates: Sequence[_TransitionGate],
    audit_store: AuditStore,
) -> None:
    """Evaluate the transition's preconditions in order; reject on the first one that fails.

    On the first gate that is not allowed, records a `rejected` audit event with that gate's
    `reason_code`, `from_status` as both the from and to status (a rejected call changes nothing),
    and `affected_node_ids` of just the root Policy id, then raises the gate's error. When every
    gate passes it returns and the caller runs the audited cascade.

    Args:
        actor: The caller's `(sub, iss)` identity, recorded on the `rejected` event.
        policy_id: Id of the Policy being transitioned.
        spec: The audit action and statuses; `from_status` is recorded on a rejection.
        gates: The preconditions, already evaluated, in checking order.
        audit_store: Where the `rejected` event is recorded as a standalone row.

    Raises:
        Exception: the `error` carried by the first gate that is not allowed.
        AuditPostgresUnavailableError: the `rejected` event could not be written (propagated).
        AuditPersistenceError: the audit insert failed (propagated).

    Side Effects:
        Records at most one `rejected` audit event.
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
    """Resulting state of a successful `approve_policy` call.

    Attributes:
        policy_id: The approved Policy's id.
        status: Always `"approved"`.
        standard_ids: Ids of every Standard in the Policy's tree.
        control_ids: Ids of every Control in the Policy's tree.
        auto_deprecated_policy_id: Id of the superseded Policy that was moved to `"deprecated"`,
            or `None` when none was.
        governed_capability_ids: Capabilities moved from the superseded Policy to this one; empty
            when none were.
    """

    policy_id: str
    status: Literal["approved"]
    standard_ids: tuple[str, ...]
    control_ids: tuple[str, ...]
    auto_deprecated_policy_id: str | None
    governed_capability_ids: tuple[str, ...] = ()


def _read_repoint(graph: GraphHandle, policy_id: str) -> _Repoint | None:
    """Return the Capability move needed to approve a fork, or `None` for a plain cascade.

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: Id of the Policy about to be approved.

    Returns:
        A `_Repoint` naming the superseded Policy and the Capabilities it governs; `None` when
        `policy_id` is not a fork or its prior governs nothing (such a fork stays approvable).

    Raises:
        redis.exceptions.RedisError: the graph read failed (propagated unwrapped).
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
    enforce_policy_manager: Callable[[tuple[str, str]], None],
    submitter: LifecycleSubmitter,
) -> PolicyApproveResult:
    """Approve a proposed Policy, moving its whole tree to `status="approved"`.

    Approving a successor also deprecates the `approved` Policy it supersedes (its own tree moves
    to `"deprecated"`) and moves that prior's governed Capabilities to the successor.

    Business Rules:
        - The caller must hold `PolicyManager` or above. This check runs after the Policy is read
          (so an unknown id is reported first); the injected enforcer audits a denial.
        - A caller may never approve a Policy they own; the owner comparison uses the full
          `(subject, issuer)` pair, so the same subject under another issuer is a different person.
        - The Policy must be `"proposed"`.
        - A failure of the last two gates records a `rejected` audit event with `reason_code`
          `self_approval_blocked` or `invalid_status` before raising.
        - Auto-deprecation runs only after the successor's cascade succeeded. A `SUPERSEDED_BY`
          prior that is not `"approved"` is left untouched. Deprecation is audited as a separate
          `policy.auto_deprecate` event on the prior, not folded into `policy.approve`.
        - If the deprecation step fails, the successor stays approved.

    Args:
        actor: The approving caller's verified `(sub, iss)` identity.
        policy_id: Id of the Policy to approve.
        graph: The single-tenant policy graph handle.
        audit_store: Where `policy.approve` and `policy.auto_deprecate` events are recorded as
            standalone rows.
        enforce_policy_manager: Called with `actor` to enforce the `PolicyManager` role. It raises
            `AccessDeniedError` (or `AuthorizationStoreUnavailableError`) and may audit the denial.
            The caller supplies it, so this service builds no role store or audit context.
        submitter: The Graph Write Gateway used for the cascades and for backfilling a legacy
            tree; the backfill only writes when some node needs it.

    Returns:
        A `PolicyApproveResult` with the new status, the tree's ids, the auto-deprecated prior's id
        (or `None`) and the Capabilities moved to the approved Policy.

    Raises:
        PolicyNotFoundError: no Policy has `policy_id`.
        AccessDeniedError: `actor` does not hold `PolicyManager` or above
            (`ps_service.api.errors.AccessDeniedError`).
        AuthorizationStoreUnavailableError: the enforcer could not read the access-role store
            (propagated).
        PolicySelfApprovalBlockedError: `actor` owns the Policy.
        PolicyInvalidStatusTransitionError: the Policy is not currently `"proposed"`.
        PolicyGovernanceConflictError: the prior's governed Capabilities changed between the read
            and the guarded write; nothing was moved or approved, and a `failed` event
            (`reason_code="governance_conflict"`) follows the `applied` one.
        PolicyLifecycleGraphUnavailableError: a graph write failed after its `applied` event; a
            `failed` follow-up event for the same action and resource is recorded first.
        redis.exceptions.RedisError: a graph read failed (propagated unwrapped).
        AuditPostgresUnavailableError: an audit event could not be written (propagated).
        AuditPersistenceError: an audit insert failed (propagated).

    Side Effects:
        Records `policy.approve` audit events (`rejected`, or `applied` then optionally `failed`)
        and, when a prior is deprecated, `policy.auto_deprecate` events. Writes the status cascade
        (and the Capability move for a fork) as one gateway group per transition. May backfill
        missing `version` and `status`.
    """
    record = _read_transition_target(graph, policy_id, submitter_source=_eager(submitter))

    owner = (record.owner_subject, record.owner_issuer)
    current_status = cast("PolicyStatus", record.status)
    affected_node_ids = _tree_node_ids(record)
    context = PolicyLifecycleRuleContext(
        actor=actor, owner=owner, action="approve", current_status=current_status
    )

    # The injected enforcer raises `AccessDeniedError` itself (and the request gate it wraps audits
    # the denial as `access.gate`), so it is deliberately not one of the audited `gates` below.
    enforce_policy_manager(actor)

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
        submitter=submitter,
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
                submitter=submitter,
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
    """Resulting state of a successful `reject_policy` call.

    Attributes:
        policy_id: The rejected Policy's id.
        status: Always `"draft"`.
        standard_ids: Ids of every Standard in the Policy's tree.
        control_ids: Ids of every Control in the Policy's tree.
    """

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
    enforce_policy_manager: Callable[[tuple[str, str]], None],
    submitter: LifecycleSubmitter,
) -> PolicyRejectResult:
    """Reject a proposed Policy, returning its whole tree to `status="draft"`.

    Uses the same gates as `approve_policy` but never auto-deprecates a prior Policy.

    Business Rules:
        - The caller must hold `PolicyManager` or above. This check runs after the Policy is read
          (so an unknown id is reported first); the injected enforcer audits a denial.
        - A caller may never reject a Policy they own; the owner comparison uses the full
          `(subject, issuer)` pair.
        - The Policy must be `"proposed"`.
        - A failure of the last two gates records a `rejected` audit event with `reason_code`
          `self_approval_blocked` or `invalid_status` before raising.

    Args:
        actor: The rejecting caller's verified `(sub, iss)` identity.
        policy_id: Id of the Policy to reject.
        graph: The single-tenant policy graph handle.
        audit_store: Where `policy.reject` events are recorded as standalone rows.
        enforce_policy_manager: Called with `actor` to enforce the `PolicyManager` role. It raises
            `AccessDeniedError` (or `AuthorizationStoreUnavailableError`) and may audit the denial.
            The caller supplies it, so this service builds no role store or audit context.
        submitter: The Graph Write Gateway used for the cascade and for backfilling a legacy tree;
            the backfill only writes when some node needs it.

    Returns:
        A `PolicyRejectResult` with the Policy's id, the new status and the ids of its tree.

    Raises:
        PolicyNotFoundError: no Policy has `policy_id`.
        AccessDeniedError: `actor` does not hold `PolicyManager` or above
            (`ps_service.api.errors.AccessDeniedError`).
        AuthorizationStoreUnavailableError: the enforcer could not read the access-role store
            (propagated).
        PolicySelfApprovalBlockedError: `actor` owns the Policy.
        PolicyInvalidStatusTransitionError: the Policy is not currently `"proposed"`.
        PolicyLifecycleGraphUnavailableError: a graph write failed after its `applied` event; a
            `failed` follow-up event for the same action and resource is recorded first.
        redis.exceptions.RedisError: a graph read failed (propagated unwrapped).
        AuditPostgresUnavailableError: an audit event could not be written (propagated).
        AuditPersistenceError: an audit insert failed (propagated).

    Side Effects:
        Records `policy.reject` audit events (`rejected`, or `applied` then optionally `failed`).
        On success sets `status` to `"draft"` on the Policy and every Standard and Control as one
        gateway group. May backfill missing `version` and `status`.
    """
    record = _read_transition_target(graph, policy_id, submitter_source=_eager(submitter))

    owner = (record.owner_subject, record.owner_issuer)
    current_status = cast("PolicyStatus", record.status)
    affected_node_ids = _tree_node_ids(record)
    context = PolicyLifecycleRuleContext(
        actor=actor, owner=owner, action="reject", current_status=current_status
    )

    # The injected enforcer raises `AccessDeniedError` itself (and the request gate it wraps audits
    # the denial as `access.gate`), so it is deliberately not one of the audited `gates` below.
    enforce_policy_manager(actor)

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
        submitter=submitter,
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
    """Resulting state of a successful `revert_policy_to_draft` call.

    Attributes:
        policy_id: The reverted Policy's id.
        status: Always `"draft"`.
        standard_ids: Ids of every Standard in the Policy's tree.
        control_ids: Ids of every Control in the Policy's tree.
    """

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
    submitter: LifecycleSubmitter,
) -> PolicyRevertResult:
    """Revert a proposed Policy to `status="draft"`, moving its whole tree back to draft.

    Unlike `approve_policy` and `reject_policy` this action is owner-only and has no role check: a
    `PolicyManager` who is not the owner is refused.

    Business Rules:
        - The caller must be the Policy's owner, else `reason_code="access_denied"`.
        - The Policy must be `"proposed"`, else `reason_code="invalid_status"`.
        - A failing gate records a `rejected` audit event before raising.

    Args:
        actor: The reverting caller's verified `(sub, iss)` identity; must be the Policy's owner.
        policy_id: Id of the Policy to revert.
        graph: The single-tenant policy graph handle.
        audit_store: Where `policy.revert` events are recorded as standalone rows.
        submitter: The Graph Write Gateway used for the cascade and for backfilling a legacy tree;
            the backfill only writes when some node needs it.

    Returns:
        A `PolicyRevertResult` with the Policy's id, the new status and the ids of its tree.

    Raises:
        PolicyNotFoundError: no Policy has `policy_id`.
        PolicyDraftAccessDeniedError: `actor` is not the Policy's owner, including when `actor`
            holds `PolicyManager`.
        PolicyInvalidStatusTransitionError: the Policy is not currently `"proposed"`.
        PolicyLifecycleGraphUnavailableError: a graph write failed after its `applied` event; a
            `failed` follow-up event for the same action and resource is recorded first.
        redis.exceptions.RedisError: a graph read failed (propagated unwrapped).
        AuditPostgresUnavailableError: an audit event could not be written (propagated).
        AuditPersistenceError: an audit insert failed (propagated).

    Side Effects:
        Records `policy.revert` audit events (`rejected`, or `applied` then optionally `failed`).
        On success sets `status` to `"draft"` on the Policy and every Standard and Control as one
        gateway group. May backfill missing `version` and `status`.
    """
    record = _read_transition_target(graph, policy_id, submitter_source=_eager(submitter))

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
        submitter=submitter,
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
    """Check that the caller may edit a draft: owner or elevated role, and the node is a draft.

    The role lookup happens only when the caller is not the owner.

    Args:
        context: The call's facts, built with `action="edit"`; `current_status` is the status of
            the node being edited.
        access_role_store: Where `context.actor`'s active roles are resolved from.

    Raises:
        PolicyDraftAccessDeniedError: `context.actor` is neither the owner nor a `SystemOwner` or
            `SystemAdmin`.
        PolicyInvalidStatusTransitionError: `context.current_status` is not `"draft"`.
        AuthorizationStoreUnavailableError: the enforcer could not read the access-role store
            (propagated).
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
    """Resulting state of a successful `update_policy_draft` call.

    Attributes:
        policy_id: The patched Policy's id.
        updated_fields: Names of the fields supplied, sorted.
    """

    policy_id: str
    updated_fields: tuple[str, ...]


def update_policy_draft(
    *,
    actor: tuple[str, str],
    policy_id: str,
    fields: Mapping[str, object],
    graph: GraphHandle,
    access_role_store: AccessRoleStore,
    submitter: LifecycleSubmitter,
) -> PolicyDraftUpdateResult:
    """Patch a subset of a draft Policy's own content fields.

    Exactly the supplied fields change: an omitted field keeps its value and a field supplied as
    `None` is cleared. The caller must be the Policy's owner or hold `SystemOwner`/`SystemAdmin`,
    and the Policy must be a draft. Draft edits write no audit event.

    Args:
        actor: The editing caller's verified `(sub, iss)` identity.
        policy_id: Id of the draft Policy to patch.
        fields: Field name to new value, or `None` to clear. The caller must already have restricted
            the keys to the patchable Policy fields; this function does not re-validate them.
        graph: The single-tenant policy graph handle.
        access_role_store: Where `actor`'s active roles are resolved from; consulted only when
            `actor` is not the owner.
        submitter: The Graph Write Gateway the patch is submitted through, also used to backfill a
            legacy tree if needed.

    Returns:
        A `PolicyDraftUpdateResult` with the Policy id and the sorted names of the supplied fields.

    Raises:
        PolicyNotFoundError: no Policy has `policy_id`.
        PolicyDraftAccessDeniedError: `actor` is neither the owner nor a
            `SystemOwner`/`SystemAdmin`.
        PolicyInvalidStatusTransitionError: the Policy is not currently `"draft"`.
        PolicyLifecycleGraphUnavailableError: the graph write failed (`FalkorDB write failed: ...`,
            with the gateway error chained); no audit event is recorded.
        AuthorizationStoreUnavailableError: the enforcer could not read the access-role store
            (propagated).
        redis.exceptions.RedisError: a graph read failed (propagated unwrapped).

    Side Effects:
        Writes the patch as one gateway group with no audit link; a patch that changes nothing logs
        nothing. May backfill missing `version` and `status`.
    """
    record = _read_transition_target(graph, policy_id, submitter_source=_eager(submitter))
    context = PolicyLifecycleRuleContext(
        actor=actor,
        owner=(record.owner_subject, record.owner_issuer),
        action="edit",
        current_status=cast("PolicyStatus", record.status),
    )
    _authorize_draft_edit(context, access_role_store=access_role_store)
    graph_writer.update_policy_fields(
        GraphWriteTarget(submitter, single_tenant_graph_name()),
        policy_id=policy_id,
        properties=fields,
    )
    return PolicyDraftUpdateResult(policy_id=policy_id, updated_fields=tuple(sorted(fields)))


@dataclass(frozen=True, slots=True)
class StandardDraftResult:
    """Resulting state of a successful `add_standard_to_draft` or `update_standard_draft` call.

    Attributes:
        standard_id: The Standard's id.
        policy_id: Id of the Standard's parent Policy.
        title: The Standard's title.
        status: Always `"draft"`.
    """

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
    submitter: LifecycleSubmitter,
) -> StandardDraftResult:
    """Add a new Standard under a draft Policy.

    The Standard's id is derived from the Policy id and `title`, and the Standard is created with
    `status="draft"`. The caller must be the parent Policy's owner or hold `SystemOwner` or
    `SystemAdmin`, and the Policy must be a draft. Draft edits write no audit event.

    Args:
        actor: The editing caller's verified `(sub, iss)` identity.
        policy_id: Id of the draft Policy to attach the Standard to.
        title: The new Standard's title.
        fields: Further content fields to set, already restricted by the caller to the patchable
            Standard fields; `implementation_status` defaults to `"draft"` when absent.
        graph: The single-tenant policy graph handle.
        access_role_store: Where `actor`'s active roles are resolved from; consulted only when
            `actor` is not the Policy's owner.
        submitter: The Graph Write Gateway the write is submitted through, also used to backfill a
            legacy tree if needed.

    Returns:
        A `StandardDraftResult` with the new Standard's id (usable in the next call), its parent
        Policy id, its title and its status.

    Raises:
        PolicyNotFoundError: no Policy has `policy_id`.
        PolicyDraftAccessDeniedError: `actor` is neither the owner nor a
            `SystemOwner`/`SystemAdmin`.
        PolicyInvalidStatusTransitionError: the Policy is not currently `"draft"`.
        PolicyLifecycleGraphUnavailableError: the graph write failed (`FalkorDB write failed: ...`,
            with the gateway error chained).
        AuthorizationStoreUnavailableError: the enforcer could not read the access-role store
            (propagated).
        redis.exceptions.RedisError: a graph read failed (propagated unwrapped).

    Side Effects:
        Writes the Standard and its link to the Policy as one gateway group with no audit link.
        May backfill missing `version` and `status`.
    """
    record = _read_transition_target(graph, policy_id, submitter_source=_eager(submitter))
    context = PolicyLifecycleRuleContext(
        actor=actor,
        owner=(record.owner_subject, record.owner_issuer),
        action="edit",
        current_status=cast("PolicyStatus", record.status),
    )
    _authorize_draft_edit(context, access_role_store=access_role_store)
    new_standard_id = standard_id(policy_id, title)
    graph_writer.add_standard_to_policy(
        GraphWriteTarget(submitter, single_tenant_graph_name()),
        policy_id=policy_id,
        standard_id=new_standard_id,
        title=title,
        extra_properties=fields,
    )
    return StandardDraftResult(
        standard_id=new_standard_id, policy_id=policy_id, title=title, status="draft"
    )


def _read_standard_with_parent_backfilled(
    graph: GraphHandle, standard_id: str, *, submitter_source: Callable[[], LifecycleSubmitter]
) -> graph_writer.StandardWithParent:
    """Read a Standard with its parent Policy's owner and status, backfilling first.

    A Standard created before statuses were backfilled can carry no `status`. The parent Policy
    id is only known after the first read, so this reads once to find it, backfills that Policy's
    tree, then reads again so the draft check that follows sees the filled-in status rather than
    a missing one.

    Args:
        graph: The single-tenant policy graph handle.
        standard_id: Id of the Standard to read.
        submitter_source: Returns the gateway for the backfill; called only when a fill is needed.

    Returns:
        The re-read `StandardWithParent` row, after the backfill.

    Raises:
        PolicyStandardNotFoundError: no Policy is linked to `standard_id` by `SUPPORTED_BY`.
        PolicyLifecycleGraphUnavailableError: a needed backfill write failed.
        redis.exceptions.RedisError: a graph read failed (propagated unwrapped).
    """
    row = graph_writer.find_standard_with_parent(graph, standard_id)
    if row is None:
        raise PolicyStandardNotFoundError(standard_id)
    graph_writer.backfill_governance_status(graph, row.policy_id, submitter_source=submitter_source)
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
    submitter: LifecycleSubmitter,
) -> StandardDraftResult:
    """Patch a subset of a draft Standard's own content fields.

    Exactly the supplied fields change: an omitted field keeps its value and a field supplied as
    `None` is cleared. Ownership comes from the parent Policy, because a Standard has no owner of
    its own; the draft check applies to the Standard's own status. The caller must be the parent
    Policy's owner or hold `SystemOwner`/`SystemAdmin`. Draft edits write no audit event.

    Args:
        actor: The editing caller's verified `(sub, iss)` identity.
        standard_id: Id of the draft Standard to patch.
        fields: Field name to new value, or `None` to clear. The caller must already have restricted
            the keys to the patchable Standard fields; this function does not re-validate them.
        graph: The single-tenant policy graph handle.
        access_role_store: Where `actor`'s active roles are resolved from; consulted only when
            `actor` is not the parent Policy's owner.
        submitter: The Graph Write Gateway the write is submitted through, also used to backfill a
            legacy tree if needed.

    Returns:
        A `StandardDraftResult` with the Standard's id, parent Policy id and title, and status
        `"draft"` (the draft check requires it before the patch).

    Raises:
        PolicyStandardNotFoundError: no Policy is linked to `standard_id` by `SUPPORTED_BY`.
        PolicyDraftAccessDeniedError: `actor` is neither the parent Policy's owner nor a
            `SystemOwner`/`SystemAdmin`.
        PolicyInvalidStatusTransitionError: the Standard is not currently `"draft"`.
        PolicyLifecycleGraphUnavailableError: the graph write failed (`FalkorDB write failed: ...`,
            with the gateway error chained).
        AuthorizationStoreUnavailableError: the enforcer could not read the access-role store
            (propagated).
        redis.exceptions.RedisError: a graph read failed (propagated unwrapped).

    Side Effects:
        Writes the patch as one gateway group with no audit link. May backfill missing `version`
        and `status`.
    """
    row = _read_standard_with_parent_backfilled(
        graph, standard_id, submitter_source=_eager(submitter)
    )
    context = PolicyLifecycleRuleContext(
        actor=actor,
        owner=(row.policy_owner_subject, row.policy_owner_issuer),
        action="edit",
        current_status=cast("PolicyStatus", row.standard_status),
    )
    _authorize_draft_edit(context, access_role_store=access_role_store)
    graph_writer.update_standard_fields(
        GraphWriteTarget(submitter, single_tenant_graph_name()),
        standard_id=standard_id,
        properties=fields,
    )
    return StandardDraftResult(
        standard_id=standard_id, policy_id=row.policy_id, title=row.standard_title, status="draft"
    )


@dataclass(frozen=True, slots=True)
class ControlDraftResult:
    """Resulting state of a successful `add_control_to_draft` or `update_control_draft` call.

    Attributes:
        control_id: The Control's id.
        standard_id: Id of the Control's parent Standard.
        policy_id: Id of the root Policy.
        title: The Control's title.
        status: Always `"draft"`.
    """

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
    submitter: LifecycleSubmitter,
) -> ControlDraftResult:
    """Add a new Control under a draft Standard.

    The Control's id is derived from the Standard id and `title`; it is created with
    `status="draft"` and linked to the Standard. Ownership comes from the root Policy, and the
    draft check applies to the parent Standard's status. The caller must be the root Policy's
    owner or hold `SystemOwner`/`SystemAdmin`. Draft edits write no audit event.

    Args:
        actor: The editing caller's verified `(sub, iss)` identity.
        standard_id: Id of the draft Standard to attach the Control to.
        title: The new Control's title.
        control_type: The Control's `type`, `"automated"` or `"manual"` (default `"manual"`),
            already validated by the caller.
        fields: Further content fields to set, already restricted by the caller to the patchable
            Control fields without `type` (which is set only through `control_type`);
            `implementation_status` defaults to `"planned"` when absent.
        graph: The single-tenant policy graph handle.
        access_role_store: Where `actor`'s active roles are resolved from; consulted only when
            `actor` is not the root Policy's owner.
        submitter: The Graph Write Gateway the write is submitted through, also used to backfill a
            legacy tree if needed.

    Returns:
        A `ControlDraftResult` with the new Control's id (usable in the next call), its parent
        Standard and Policy ids, its title and its status.

    Raises:
        PolicyStandardNotFoundError: no Policy is linked to `standard_id` by `SUPPORTED_BY`.
        PolicyDraftAccessDeniedError: `actor` is neither the root Policy's owner nor a
            `SystemOwner`/`SystemAdmin`.
        PolicyInvalidStatusTransitionError: the Standard is not currently `"draft"`.
        PolicyLifecycleGraphUnavailableError: the graph write failed (`FalkorDB write failed: ...`,
            with the gateway error chained).
        AuthorizationStoreUnavailableError: the enforcer could not read the access-role store
            (propagated).
        redis.exceptions.RedisError: a graph read failed (propagated unwrapped).

    Side Effects:
        Writes the Control and its link to the Standard as one gateway group with no audit link.
        May backfill missing `version` and `status`.
    """
    row = _read_standard_with_parent_backfilled(
        graph, standard_id, submitter_source=_eager(submitter)
    )
    context = PolicyLifecycleRuleContext(
        actor=actor,
        owner=(row.policy_owner_subject, row.policy_owner_issuer),
        action="edit",
        current_status=cast("PolicyStatus", row.standard_status),
    )
    _authorize_draft_edit(context, access_role_store=access_role_store)
    new_control_id = control_id(standard_id, title)
    graph_writer.add_control_to_standard(
        GraphWriteTarget(submitter, single_tenant_graph_name()),
        standard_id=standard_id,
        control_id=new_control_id,
        title=title,
        control_type=control_type,
        extra_properties=fields,
    )
    return ControlDraftResult(
        control_id=new_control_id,
        standard_id=standard_id,
        policy_id=row.policy_id,
        title=title,
        status="draft",
    )


def _read_control_with_parent_backfilled(
    graph: GraphHandle, control_id: str, *, submitter_source: Callable[[], LifecycleSubmitter]
) -> graph_writer.ControlWithParent:
    """Read a Control with its parent Standard and root Policy, backfilling first.

    A Control created before statuses were backfilled can carry no `status` even though its root
    Policy is a draft. The root Policy id is only known after a two-hop read, so this reads once to
    find it, backfills that Policy's tree, then reads again so the draft check that follows sees the
    filled-in status.

    Args:
        graph: The single-tenant policy graph handle.
        control_id: Id of the Control to read.
        submitter_source: Returns the gateway for the backfill; called only when a fill is needed.

    Returns:
        The re-read `ControlWithParent` row, after the backfill.

    Raises:
        PolicyControlNotFoundError: no Policy reaches `control_id` through `SUPPORTED_BY` then
            `IMPLEMENTED_BY`.
        PolicyLifecycleGraphUnavailableError: a needed backfill write failed.
        redis.exceptions.RedisError: a graph read failed (propagated unwrapped).
    """
    row = graph_writer.find_control_with_parent(graph, control_id)
    if row is None:
        raise PolicyControlNotFoundError(control_id)
    graph_writer.backfill_governance_status(graph, row.policy_id, submitter_source=submitter_source)
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
    submitter: LifecycleSubmitter,
) -> ControlDraftResult:
    """Patch a subset of a draft Control's own content fields.

    Exactly the supplied fields change: an omitted field keeps its value and a field supplied as
    `None` is cleared. Ownership comes from the root Policy, because neither a Control nor its
    Standard has an owner of its own; the draft check applies to the Control's own status. The
    caller must be the root Policy's owner or hold `SystemOwner`/`SystemAdmin`. Draft edits write
    no audit event.

    Args:
        actor: The editing caller's verified `(sub, iss)` identity.
        control_id: Id of the draft Control to patch.
        fields: Field name to new value, or `None` to clear. The caller must already have restricted
            the keys to the patchable Control fields; this function does not re-validate them.
        graph: The single-tenant policy graph handle.
        access_role_store: Where `actor`'s active roles are resolved from; consulted only when
            `actor` is not the root Policy's owner.
        submitter: The Graph Write Gateway the write is submitted through, also used to backfill a
            legacy tree if needed.

    Returns:
        A `ControlDraftResult` with the Control's id, parent Standard id, root Policy id and title,
        and status `"draft"` (the draft check requires it before the patch).

    Raises:
        PolicyControlNotFoundError: no Policy reaches `control_id` through `SUPPORTED_BY` then
            `IMPLEMENTED_BY`.
        PolicyDraftAccessDeniedError: `actor` is neither the root Policy's owner nor a
            `SystemOwner`/`SystemAdmin`.
        PolicyInvalidStatusTransitionError: the Control is not currently `"draft"`.
        PolicyLifecycleGraphUnavailableError: the graph write failed (`FalkorDB write failed: ...`,
            with the gateway error chained).
        AuthorizationStoreUnavailableError: the enforcer could not read the access-role store
            (propagated).
        redis.exceptions.RedisError: a graph read failed (propagated unwrapped).

    Side Effects:
        Writes the patch as one gateway group with no audit link. May backfill missing `version`
        and `status`.
    """
    row = _read_control_with_parent_backfilled(
        graph, control_id, submitter_source=_eager(submitter)
    )
    context = PolicyLifecycleRuleContext(
        actor=actor,
        owner=(row.policy_owner_subject, row.policy_owner_issuer),
        action="edit",
        current_status=cast("PolicyStatus", row.control_status),
    )
    _authorize_draft_edit(context, access_role_store=access_role_store)
    graph_writer.update_control_fields(
        GraphWriteTarget(submitter, single_tenant_graph_name()),
        control_id=control_id,
        properties=fields,
    )
    return ControlDraftResult(
        control_id=control_id,
        standard_id=row.standard_id,
        policy_id=row.policy_id,
        title=row.control_title,
        status="draft",
    )
