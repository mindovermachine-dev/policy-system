"""Typed models of `ps_service.graph_cleanup`: candidate groups, previews, plans and snapshots.

Frozen pydantic models with `extra="forbid"`, so an unexpected field fails validation. The
MCP tools return `model_dump()` of the result and preview models verbatim; the plan, state and
input models stay inside the component.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class GoverningPolicy(BaseModel):
    """The Policy governing a Capability (`GOVERNED_BY`), as id, title and status.

    Attributes:
        id: Policy id.
        title: Policy title.
        status: Lifecycle status of the Policy (for example `draft`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    status: str


class CapabilityRecord(BaseModel):
    """One active Capability as read from the single-tenant graph.

    Attributes:
        id: Capability id.
        name: Capability name.
        embedding: Cached embedding vector; None when none is cached (it is never computed here).
        governing_policy: The Policy governing the Capability; None when ungoverned.
        obligation_count: Number of Obligations that require the Capability; 0 or more.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    embedding: tuple[float, ...] | None = None
    governing_policy: GoverningPolicy | None = None
    obligation_count: int = 0


class CapabilityCandidateMember(BaseModel):
    """One Capability inside a candidate group.

    Attributes:
        id: Capability id.
        name: Capability name.
        obligation_count: Number of Obligations that require the Capability.
        governing_policy: The Policy governing the Capability; None when ungoverned.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    obligation_count: int
    governing_policy: GoverningPolicy | None


MergeCase = Literal[1, 2, 3]


class CapabilityCandidateGroup(BaseModel):
    """Capabilities judged to be the same duty, with the evidence basis.

    Attributes:
        basis: `name` when linked by equal normalised names, `embedding` when by cached-embedding
            similarity.
        merge_case: Governance case of merging the members: 1 none governed, 2 exactly one
            governed, 3 two or more governed.
        policies_distinct: True when the governed members name more than one Policy; such a
            group cannot be merged until governance is released.
        members: The grouped Capabilities, two or more.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    basis: Literal["name", "embedding"]
    merge_case: MergeCase
    policies_distinct: bool
    members: tuple[CapabilityCandidateMember, ...]


class FindCapabilityCandidatesResult(BaseModel):
    """`find-capability-merge-candidates` result.

    Attributes:
        groups: Candidate groups; empty when no active Capabilities look alike.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    groups: tuple[CapabilityCandidateGroup, ...]


class RequirementSourceRef(BaseModel):
    """A Requirement satisfied by an Obligation, with its `EXPRESSES` `source_ref`.

    Attributes:
        requirement_id: Requirement id.
        source_ref: The `EXPRESSES` source reference into the regulation text; None when absent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str
    source_ref: str | None


class ObligationRecord(BaseModel):
    """One Obligation as read from the single-tenant graph, with its Role.

    Attributes:
        id: Obligation id.
        text: Obligation text.
        role_id: Id of the Role that bears the Obligation.
        role_name: Name of that Role.
        requirements: The Requirements the Obligation satisfies, with their source references.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    text: str
    role_id: str
    role_name: str
    requirements: tuple[RequirementSourceRef, ...] = ()


class ObligationCandidateMember(BaseModel):
    """One Obligation inside a duplicate group.

    Attributes:
        id: Obligation id.
        text: Obligation text.
        requirements: The Requirements the Obligation satisfies, with their source references.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    text: str
    requirements: tuple[RequirementSourceRef, ...]


class ObligationCandidateGroup(BaseModel):
    """Obligations under ONE Role with identical or near-identical text.

    Attributes:
        role_id: Id of the Role all members belong to.
        role_name: Name of that Role.
        basis: `identical_text` for equal text, `near_text` for near-identical text.
        members: The grouped Obligations, two or more.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    role_id: str
    role_name: str
    basis: Literal["identical_text", "near_text"]
    members: tuple[ObligationCandidateMember, ...]


class FindDuplicateObligationsResult(BaseModel):
    """`find-duplicate-obligations` result.

    Attributes:
        groups: Duplicate groups; empty when no duplicates were found.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    groups: tuple[ObligationCandidateGroup, ...]


PropertyValue = str | int | float | bool | None


class NodeRecord(BaseModel):
    """One node of a `GraphSnapshot`: label, id and its scalar properties (no embeddings).

    Attributes:
        label: Node label.
        id: Node id.
        properties: Scalar properties of the node; vectors are not included.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    id: str
    properties: dict[str, PropertyValue]


class EdgeRecord(BaseModel):
    """One directed edge of a `GraphSnapshot`, addressed by endpoint labels and ids.

    Attributes:
        rel_type: Relationship type, such as `REQUIRES`.
        source_label: Label of the source node.
        source_id: Id of the source node.
        target_label: Label of the target node.
        target_id: Id of the target node.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rel_type: str
    source_label: str
    source_id: str
    target_label: str
    target_id: str


class GraphSnapshot(BaseModel):
    """Nodes and edges touched by one cleanup edit, sufficient to reverse it.

    Attributes:
        nodes: The nodes touched by the edit.
        edges: The edges touched by the edit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    nodes: tuple[NodeRecord, ...]
    edges: tuple[EdgeRecord, ...]


class CapabilityNodeState(BaseModel):
    """A Capability as read for a merge: id, name, effective status and scalar properties.

    Attributes:
        id: Capability id.
        name: Capability name.
        status: Effective status, `active` when the node has no `status`.
        properties: Scalar properties of the node.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    status: str
    properties: dict[str, PropertyValue]


class MergeState(BaseModel):
    """Everything the merge planner needs about the two Capabilities, read from the graph.

    Attributes:
        survivor: The surviving Capability; None when it does not exist.
        absorbed: The Capability to absorb; None when it does not exist.
        edges: Every `REQUIRES` / `COVERS` / `MITIGATED_BY` edge incident to either Capability.
        survivor_policies: Policies governing the survivor.
        absorbed_policies: Policies governing the absorbed Capability.
        governed_sets: For each involved Policy id, the ids of every Capability it governs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    survivor: CapabilityNodeState | None
    absorbed: CapabilityNodeState | None
    edges: tuple[EdgeRecord, ...]
    survivor_policies: tuple[GoverningPolicy, ...]
    absorbed_policies: tuple[GoverningPolicy, ...]
    governed_sets: dict[str, tuple[str, ...]] = {}


class EdgesToMove(BaseModel):
    """Endpoint ids (the non-Capability side) of the edges that move onto the survivor.

    Attributes:
        requires: Ids at the other end of `REQUIRES` edges.
        covers: Ids at the other end of `COVERS` edges.
        mitigated_by: Ids at the other end of `MITIGATED_BY` edges.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    requires: tuple[str, ...]
    covers: tuple[str, ...]
    mitigated_by: tuple[str, ...]


class GovernanceChange(BaseModel):
    """What a case-2 or case-3 merge does to the governing Policy.

    Case 2: exactly one capability governed (`governed_side` survivor or absorbed). Case 3:
    both governed by the same Policy (`governed_side` `both`): only the absorbed capability's
    edge is deleted and no acknowledgment is required.

    Attributes:
        policy: The governing Policy.
        governed_side: Which Capability carries the governance: `survivor`, `absorbed`, or
            `both`.
        obligations_coverage_changed: Count of Obligations that require the ungoverned
            Capability and not the governed one; they newly gain the Policy's coverage.
        governed_set_before: Ids of every Capability the Policy governs before the merge.
        governed_set_after: Ids of every Capability the Policy governs after the merge.
        acknowledgment_required: True when the officer must acknowledge the change before an
            approval is created (case 2).
        acknowledgment_text: The text the officer must accept; shown with the preview.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: GoverningPolicy
    governed_side: Literal["survivor", "absorbed", "both"]
    obligations_coverage_changed: int
    governed_set_before: tuple[str, ...]
    governed_set_after: tuple[str, ...]
    acknowledgment_required: bool
    acknowledgment_text: str


class CapabilityMergePreview(BaseModel):
    """What a Compliance Officer is shown before approving a Capability merge.

    Attributes:
        survivor_id: Id of the Capability that remains.
        survivor_name: Name of the surviving Capability.
        absorbed_id: Id of the Capability that is deleted and replaced by a marker.
        absorbed_name: Name of the absorbed Capability.
        policy_case: Governance case: 1 none governed, 2 exactly one governed, 3 both governed
            by the same Policy.
        edges_to_move: Edge endpoints that move onto the survivor.
        duplicate_edges_collapsed: Count of absorbed edges the survivor already has.
        obligations_affected: Count of Obligations whose `REQUIRES` edge moves to the survivor.
        state_digest: sha256 of the previewed state; binds an approval to what was shown.
        governance: Effect on the governing Policy; None in case 1.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    survivor_id: str
    survivor_name: str
    absorbed_id: str
    absorbed_name: str
    policy_case: MergeCase
    edges_to_move: EdgesToMove
    duplicate_edges_collapsed: int
    obligations_affected: int
    state_digest: str
    governance: GovernanceChange | None = None


class ExpectedCounts(BaseModel):
    """Per-class edge counts that the guarded Capability-merge write pins.

    The write is refused as stale when the live counts differ.

    Attributes:
        requires: Expected `REQUIRES` edges on the absorbed Capability.
        covers: Expected `COVERS` edges on the absorbed Capability.
        mitigated: Expected `MITIGATED_BY` edges on the absorbed Capability.
        absorbed_governed: Expected `GOVERNED_BY` edges on the absorbed Capability (0 or 1).
        survivor_governed: Expected `GOVERNED_BY` edges on the survivor (0 or 1).
        absorbed_policy_id: Policy id governing the absorbed Capability; None if ungoverned.
        absorbed_policy_status: Status of that Policy; None if ungoverned.
        survivor_policy_id: Policy id governing the survivor; None if ungoverned.
        survivor_policy_status: Status of that Policy; None if ungoverned.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    requires: int
    covers: int
    mitigated: int
    absorbed_governed: int = 0
    survivor_governed: int = 0
    absorbed_policy_id: str | None = None
    absorbed_policy_status: str | None = None
    survivor_policy_id: str | None = None
    survivor_policy_status: str | None = None


class CapabilityMergePlan(BaseModel):
    """Pure result of planning one Capability merge.

    Attributes:
        preview: What the officer is shown.
        expected: Counts the guarded write pins.
        before: Snapshot of the touched nodes and edges before the merge.
        after: Snapshot after the merge.
        state_digest: sha256 of the previewed state.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    preview: CapabilityMergePreview
    expected: ExpectedCounts
    before: GraphSnapshot
    after: GraphSnapshot
    state_digest: str


class RoleRef(BaseModel):
    """A Role by id and name, as the bearer of an Obligation.

    Attributes:
        id: Role id.
        name: Role name.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str


class ObligationNodeState(BaseModel):
    """An Obligation as read for a merge: id, text and its other scalar properties.

    Attributes:
        id: Obligation id.
        text: Obligation text.
        properties: Other scalar properties of the node.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    text: str
    properties: dict[str, PropertyValue]


class ObligationMergeState(BaseModel):
    """Everything the Obligation-merge planner needs, read from the graph.

    The Role of each side is its inbound `HAS` source(s); a well-formed Obligation has exactly
    one.

    Attributes:
        survivor: The surviving Obligation; None when it does not exist.
        absorbed: The Obligation to absorb; None when it does not exist.
        survivor_roles: Roles that `HAS` the survivor.
        absorbed_roles: Roles that `HAS` the absorbed Obligation.
        edges: Every `SATISFIED_BY` (Requirement -> Obligation) and `REQUIRES`
            (Obligation -> Capability) edge incident to either Obligation.
        absorbed_requirement_refs: Source references of the absorbed Obligation's Requirements.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    survivor: ObligationNodeState | None
    absorbed: ObligationNodeState | None
    survivor_roles: tuple[RoleRef, ...]
    absorbed_roles: tuple[RoleRef, ...]
    edges: tuple[EdgeRecord, ...]
    absorbed_requirement_refs: tuple[RequirementSourceRef, ...] = ()


class ObligationEdgesToMove(BaseModel):
    """Endpoint ids that union onto the survivor: Requirement ids and Capability ids.

    Attributes:
        satisfied_by: Requirement ids on `SATISFIED_BY` edges.
        requires: Capability ids on `REQUIRES` edges.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    satisfied_by: tuple[str, ...]
    requires: tuple[str, ...]


class ObligationMergePreview(BaseModel):
    """What a Compliance Officer is shown before approving an Obligation merge.

    Attributes:
        survivor_id: Id of the Obligation that remains.
        survivor_text: Text of the surviving Obligation.
        absorbed_id: Id of the Obligation that is deleted.
        absorbed_text: Text of the absorbed Obligation.
        role_id: Id of the Role both Obligations belong to.
        role_name: Name of that Role.
        edges_to_move: Edge endpoints that union onto the survivor.
        duplicate_edges_collapsed: Count of absorbed edges the survivor already has.
        requirement_source_refs: Source references of the absorbed Obligation's Requirements.
        state_digest: sha256 of the previewed state; binds an approval to what was shown.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    survivor_id: str
    survivor_text: str
    absorbed_id: str
    absorbed_text: str
    role_id: str
    role_name: str
    edges_to_move: ObligationEdgesToMove
    duplicate_edges_collapsed: int
    requirement_source_refs: tuple[RequirementSourceRef, ...]
    state_digest: str


class ObligationExpectedCounts(BaseModel):
    """Per-class counts of the absorbed Obligation's edges that the guarded write pins.

    Attributes:
        satisfied: Expected `SATISFIED_BY` edges on the absorbed Obligation.
        requires: Expected `REQUIRES` edges on the absorbed Obligation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    satisfied: int
    requires: int


class ObligationMergePlan(BaseModel):
    """Pure result of planning one Obligation merge.

    Attributes:
        preview: What the officer is shown.
        expected: Counts the guarded write pins.
        before: Snapshot of the touched nodes and edges before the merge.
        after: Snapshot after the merge.
        state_digest: sha256 of the previewed state.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    preview: ObligationMergePreview
    expected: ObligationExpectedCounts
    before: GraphSnapshot
    after: GraphSnapshot
    state_digest: str


class ReleaseState(BaseModel):
    """What the reader found for `release-capability-governance`: one Capability and its policy.

    Attributes:
        capability: The Capability; None when the id does not exist.
        policies: Policies the Capability is `GOVERNED_BY` (the model allows exactly one).
        governed_set: Id of every Capability the governing Policy governs; empty when ungoverned.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: CapabilityNodeState | None
    policies: tuple[GoverningPolicy, ...]
    governed_set: tuple[str, ...] = ()


class ReleasePreview(BaseModel):
    """What a Compliance Officer is shown before approving a governance release.

    Attributes:
        capability_id: Id of the Capability to release.
        capability_name: Name of that Capability.
        policy_id: Id of the governing Policy.
        policy_title: Title of that Policy.
        policy_status: Status of that Policy; only `draft` can be released.
        governed_set_before: Ids of every Capability the Policy governs now.
        governed_set_after: The same ids without the released Capability.
        state_digest: sha256 of the previewed state; binds an approval to what was shown.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str
    capability_name: str
    policy_id: str
    policy_title: str
    policy_status: str
    governed_set_before: tuple[str, ...]
    governed_set_after: tuple[str, ...]
    state_digest: str


class ReleasePlan(BaseModel):
    """The release preview, the before/after snapshots and the state digest.

    Attributes:
        preview: What the officer is shown.
        before: Snapshot holding the `GOVERNED_BY` edge.
        after: Snapshot without it.
        state_digest: sha256 of the previewed state.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    preview: ReleasePreview
    before: GraphSnapshot
    after: GraphSnapshot
    state_digest: str


class CapabilityUnmergeInputs(BaseModel):
    """What a Capability merge's audit snapshot says to reverse, derived without I/O.

    Attributes:
        merge_approval_id: Approval id of the merge being reversed.
        survivor_id: Id of the surviving Capability.
        absorbed_id: Id of the deleted Capability the merge left a marker for.
        absorbed_properties: The absorbed Capability's scalar properties as the merge's `before`
            snapshot recorded them (`name`, `created_at` and the optional ones; id excluded).
        policy_case: Governance case of the merge being reversed.
        restore_requires: Endpoint ids of `REQUIRES` edges the merge moved off the absorbed node.
        restore_covers: The same for `COVERS` edges.
        restore_mitigated: The same for `MITIGATED_BY` edges.
        remove_requires: Subset of `restore_requires` that was not already on the survivor
            before the merge; only those leave the survivor again.
        remove_covers: The same for `COVERS` edges.
        remove_mitigated: The same for `MITIGATED_BY` edges.
        restore_policy_id: Policy id of the absorbed node's `GOVERNED_BY` edge (moved in case 2,
            deleted in case 3); None when it had none.
        remove_policy_edge: True only when that edge was moved, so the survivor loses it again.
        snapshot_survivor_edges: The survivor's in-scope incoming edges as the merge left them;
            anything else on the survivor now was added since.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    merge_approval_id: str
    survivor_id: str
    absorbed_id: str
    absorbed_properties: dict[str, PropertyValue]
    policy_case: MergeCase
    restore_requires: tuple[str, ...]
    restore_covers: tuple[str, ...]
    restore_mitigated: tuple[str, ...]
    remove_requires: tuple[str, ...]
    remove_covers: tuple[str, ...]
    remove_mitigated: tuple[str, ...]
    restore_policy_id: str | None
    remove_policy_edge: bool
    snapshot_survivor_edges: tuple[EdgeRecord, ...]


class CapabilityUnmergeState(BaseModel):
    """The live graph around a merge's marker, as `unmerge` needs it.

    Attributes:
        absorbed_status: Effective status of a Capability that exists under the absorbed id;
            None when no such node exists (the state a merge leaves).
        survivor: The surviving Capability; None when it does not exist.
        marker_targets: Maps a Capability id to the `merged_into` ids of its `MergedCapability`
            markers.
        survivor_edges: The survivor's current in-scope incoming edges.
        survivor_policy_ids: Ids of Policies governing the survivor.
        existing_endpoints: Maps each edge class to the snapshot endpoint ids that still exist.
        policy_exists: False when the Policy the absorbed node was governed by is gone.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    absorbed_status: str | None
    survivor: CapabilityNodeState | None
    marker_targets: dict[str, tuple[str, ...]]
    survivor_edges: tuple[EdgeRecord, ...]
    survivor_policy_ids: tuple[str, ...]
    existing_endpoints: dict[str, tuple[str, ...]]
    policy_exists: bool = True


class UnmergeEdgeSet(BaseModel):
    """The edges one side of an unmerge touches, as endpoint ids per class.

    Attributes:
        requires: Ids at the other end of `REQUIRES` edges.
        covers: Ids at the other end of `COVERS` edges.
        mitigated_by: Ids at the other end of `MITIGATED_BY` edges.
        governed_by: Id of the governing Policy when a `GOVERNED_BY` edge is touched; else None.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    requires: tuple[str, ...]
    covers: tuple[str, ...]
    mitigated_by: tuple[str, ...]
    governed_by: str | None = None


class CapabilityUnmergePreview(BaseModel):
    """What a Compliance Officer is shown before approving a Capability unmerge.

    Attributes:
        kind: Always `capability`; tells a client which unmerge preview it holds.
        merged_id: Id of the deleted Capability to recreate.
        merged_name: Name of that Capability.
        survivor_id: Id of the surviving Capability.
        survivor_name: Name of the survivor.
        merge_approval_id: Approval id of the merge being reversed.
        edges_to_restore: Edges put back on the restored Capability.
        edges_removed_from_survivor: Edges that leave the survivor again.
        survivor_added_edges: Edges the survivor gained since the merge; they stay.
        state_digest: sha256 of the previewed state; binds an approval to what was shown.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["capability"] = "capability"
    merged_id: str
    merged_name: str
    survivor_id: str
    survivor_name: str
    merge_approval_id: str
    edges_to_restore: UnmergeEdgeSet
    edges_removed_from_survivor: UnmergeEdgeSet
    survivor_added_edges: tuple[EdgeRecord, ...]
    state_digest: str


class CapabilityUnmergeWrite(BaseModel):
    """The exact parameters the guarded Capability-unmerge write pins and applies.

    Attributes:
        restore_requires_ids: Ids whose `REQUIRES` edge is put back on the restored Capability.
        remove_requires_ids: Ids whose `REQUIRES` edge leaves the survivor.
        restore_covers_ids: The same as `restore_requires_ids` for `COVERS`.
        remove_covers_ids: The same as `remove_requires_ids` for `COVERS`.
        restore_mitigated_ids: The same as `restore_requires_ids` for `MITIGATED_BY`.
        remove_mitigated_ids: The same as `remove_requires_ids` for `MITIGATED_BY`.
        restore_policy_id: Policy whose `GOVERNED_BY` edge is put back; None for none.
        remove_policy_edge: True when the survivor's `GOVERNED_BY` edge is removed.
        properties: Scalar properties set on the recreated Capability, `status` forced to
            `active`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    restore_requires_ids: tuple[str, ...]
    remove_requires_ids: tuple[str, ...]
    restore_covers_ids: tuple[str, ...]
    remove_covers_ids: tuple[str, ...]
    restore_mitigated_ids: tuple[str, ...]
    remove_mitigated_ids: tuple[str, ...]
    restore_policy_id: str | None
    remove_policy_edge: bool
    properties: dict[str, PropertyValue]


class CapabilityUnmergePlan(BaseModel):
    """Pure result of planning one Capability unmerge.

    Attributes:
        preview: What the officer is shown.
        write: Parameters the guarded write applies.
        restored_edges: Exactly the edges put back on the restored Capability.
        before: Snapshot of the marker and survivor state.
        after: Snapshot of the restored state.
        state_digest: sha256 of the previewed state.
        inputs: What the merge's audit snapshot said to reverse.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    preview: CapabilityUnmergePreview
    write: CapabilityUnmergeWrite
    restored_edges: tuple[EdgeRecord, ...]
    before: GraphSnapshot
    after: GraphSnapshot
    state_digest: str
    inputs: CapabilityUnmergeInputs


class ObligationUnmergeInputs(BaseModel):
    """What an Obligation merge's audit snapshot says to recreate, derived without I/O.

    Attributes:
        merge_approval_id: Approval id of the merge being reversed.
        survivor_id: Id of the surviving Obligation.
        absorbed_id: Id of the deleted Obligation to recreate.
        role_id: Id of the Role the Obligation belongs to.
        properties: The absorbed Obligation's scalar properties (its `text` included, its id
            excluded).
        satisfied_by_ids: Requirements that satisfied the absorbed Obligation.
        requires_ids: Capabilities the absorbed Obligation required.
        snapshot_survivor_edges: The survivor's `SATISFIED_BY` and `REQUIRES` edges as the merge
            left them.
        survivor_edges_before_merge: The same edges as they were before the merge, so edges
            added since and edges that may come from the merge can be told apart.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    merge_approval_id: str
    survivor_id: str
    absorbed_id: str
    role_id: str
    properties: dict[str, PropertyValue]
    satisfied_by_ids: tuple[str, ...]
    requires_ids: tuple[str, ...]
    snapshot_survivor_edges: tuple[EdgeRecord, ...]
    survivor_edges_before_merge: tuple[EdgeRecord, ...]


class ObligationUnmergeState(BaseModel):
    """The live graph around a deleted Obligation, as `unmerge` needs it.

    Attributes:
        absorbed_exists: True when the absorbed Obligation exists again.
        survivor_exists: True when the survivor exists.
        survivor_text: Current text of the survivor.
        marker_targets: Maps an Obligation id to the `merged_into` ids of its `MergedObligation`
            markers.
        role_exists: True when the Role still exists.
        existing_requirements: Ids of the snapshot Requirements that still exist.
        existing_capabilities: Ids of the snapshot Capabilities that still exist.
        merged_capability_targets: Maps a snapshot Capability that no longer exists to the
            `merged_into` ids of its `MergedCapability` markers (empty when it simply vanished).
        survivor_edges: The survivor's current `SATISFIED_BY` / `REQUIRES` edges.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    absorbed_exists: bool
    survivor_exists: bool
    survivor_text: str
    marker_targets: dict[str, tuple[str, ...]]
    role_exists: bool
    existing_requirements: tuple[str, ...]
    existing_capabilities: tuple[str, ...]
    merged_capability_targets: dict[str, tuple[str, ...]]
    survivor_edges: tuple[EdgeRecord, ...]


class ObligationUnmergeEdgeSet(BaseModel):
    """Endpoint ids of the edges an Obligation unmerge recreates.

    Attributes:
        satisfied_by: Requirement ids on `SATISFIED_BY` edges.
        requires: Capability ids on `REQUIRES` edges.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    satisfied_by: tuple[str, ...]
    requires: tuple[str, ...]


class ObligationUnmergePreview(BaseModel):
    """What a Compliance Officer is shown before approving an Obligation unmerge.

    Attributes:
        kind: Always `obligation`; tells a client which unmerge preview it holds.
        merged_id: Id of the deleted Obligation to recreate.
        merged_text: Text the recreated Obligation gets.
        survivor_id: Id of the surviving Obligation.
        survivor_text: Text of the survivor.
        role_id: Id of the Role the recreated Obligation joins.
        merge_approval_id: Approval id of the merge being reversed.
        edges_to_restore: Edges recreated on the restored Obligation.
        survivor_added_edges: Edges the survivor gained since the merge; they stay.
        survivor_edges_possibly_from_merge: Survivor edges that may have come from the absorbed
            Obligation; they also stay.
        note: Explanation to the officer of what stays on the survivor.
        state_digest: sha256 of the previewed state; binds an approval to what was shown.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["obligation"] = "obligation"
    merged_id: str
    merged_text: str
    survivor_id: str
    survivor_text: str
    role_id: str
    merge_approval_id: str
    edges_to_restore: ObligationUnmergeEdgeSet
    survivor_added_edges: tuple[EdgeRecord, ...]
    survivor_edges_possibly_from_merge: tuple[EdgeRecord, ...]
    note: str
    state_digest: str


class ObligationUnmergeWrite(BaseModel):
    """The exact parameters the guarded Obligation-unmerge write pins and applies.

    Attributes:
        role_id: Role the recreated Obligation joins.
        satisfied_by_ids: Requirement ids the recreated Obligation is `SATISFIED_BY`.
        requires_ids: Capability ids the recreated Obligation `REQUIRES`.
        properties: Scalar properties set on the recreated Obligation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    role_id: str
    satisfied_by_ids: tuple[str, ...]
    requires_ids: tuple[str, ...]
    properties: dict[str, PropertyValue]


class ObligationUnmergePlan(BaseModel):
    """Pure result of planning one Obligation unmerge.

    Attributes:
        preview: What the officer is shown.
        write: Parameters the guarded write applies.
        restored_edges: Exactly the edges put back on the recreated Obligation.
        before: Snapshot of the marker and survivor state.
        after: Snapshot of the recreated Obligation with its edges.
        state_digest: sha256 of the previewed state.
        inputs: What the merge's audit snapshot said to recreate.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    preview: ObligationUnmergePreview
    write: ObligationUnmergeWrite
    restored_edges: tuple[EdgeRecord, ...]
    before: GraphSnapshot
    after: GraphSnapshot
    state_digest: str
    inputs: ObligationUnmergeInputs
