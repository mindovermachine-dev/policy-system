"""Typed results of `ps_service.graph_cleanup` candidate discovery (issue #190).

Pydantic models with `extra="forbid"` (AC-BI-003 wire shape); the MCP tool
returns `model_dump()` of these verbatim.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class GoverningPolicy(BaseModel):
    """The Policy governing a Capability (`GOVERNED_BY`), as id, title and status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    status: str


class CapabilityRecord(BaseModel):
    """One active Capability as read from the single-tenant graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    embedding: tuple[float, ...] | None = None
    governing_policy: GoverningPolicy | None = None
    obligation_count: int = 0


class CapabilityCandidateMember(BaseModel):
    """One Capability inside a candidate group."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    obligation_count: int
    governing_policy: GoverningPolicy | None


MergeCase = Literal[1, 2, 3]


class CapabilityCandidateGroup(BaseModel):
    """Capabilities judged to be the same duty, with the evidence basis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    basis: Literal["name", "embedding"]
    merge_case: MergeCase
    policies_distinct: bool
    members: tuple[CapabilityCandidateMember, ...]


class FindCapabilityCandidatesResult(BaseModel):
    """`find-capability-merge-candidates` result: zero or more groups."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    groups: tuple[CapabilityCandidateGroup, ...]


class RequirementSourceRef(BaseModel):
    """A Requirement satisfied by an Obligation, with its `EXPRESSES` `source_ref`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str
    source_ref: str | None


class ObligationRecord(BaseModel):
    """One Obligation as read from the single-tenant graph, with its Role."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    text: str
    role_id: str
    role_name: str
    requirements: tuple[RequirementSourceRef, ...] = ()


class ObligationCandidateMember(BaseModel):
    """One Obligation inside a duplicate group."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    text: str
    requirements: tuple[RequirementSourceRef, ...]


class ObligationCandidateGroup(BaseModel):
    """Obligations under ONE Role with identical or near-identical text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role_id: str
    role_name: str
    basis: Literal["identical_text", "near_text"]
    members: tuple[ObligationCandidateMember, ...]


class FindDuplicateObligationsResult(BaseModel):
    """`find-duplicate-obligations` result: zero or more groups."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    groups: tuple[ObligationCandidateGroup, ...]


PropertyValue = str | int | float | bool | None


class NodeRecord(BaseModel):
    """One node of a `GraphSnapshot`: label, id and its scalar properties (no embeddings)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    id: str
    properties: dict[str, PropertyValue]


class EdgeRecord(BaseModel):
    """One directed edge of a `GraphSnapshot`, addressed by endpoint labels and ids."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rel_type: str
    source_label: str
    source_id: str
    target_label: str
    target_id: str


class GraphSnapshot(BaseModel):
    """Nodes and edges touched by one cleanup edit, sufficient to reverse it (AC-BI-021)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    nodes: tuple[NodeRecord, ...]
    edges: tuple[EdgeRecord, ...]


class CapabilityNodeState(BaseModel):
    """A Capability as read for a merge: id, name, effective status and scalar properties."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    status: str
    properties: dict[str, PropertyValue]


class MergeState(BaseModel):
    """Everything the merge planner needs about the two Capabilities, read from the graph.

    `edges` holds every `REQUIRES` / `COVERS` / `MITIGATED_BY` edge incident to either
    Capability. A side that does not exist is `None`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    survivor: CapabilityNodeState | None
    absorbed: CapabilityNodeState | None
    edges: tuple[EdgeRecord, ...]
    survivor_policies: tuple[GoverningPolicy, ...]
    absorbed_policies: tuple[GoverningPolicy, ...]
    governed_sets: dict[str, tuple[str, ...]] = {}


class EdgesToMove(BaseModel):
    """Endpoint ids (the non-Capability side) of the edges that move onto the survivor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requires: tuple[str, ...]
    covers: tuple[str, ...]
    mitigated_by: tuple[str, ...]


class GovernanceChange(BaseModel):
    """What a case-2 or case-3 merge does to the governing Policy.

    Case 2: exactly one capability governed (`governed_side` survivor or absorbed). Case 3:
    both governed by the same Policy (`governed_side` `both`): only the absorbed capability's
    edge is deleted and no acknowledgment is required.

    `obligations_coverage_changed` counts the Obligations that require the ungoverned
    capability and not the governed one: they newly gain the Policy's coverage.
    `governed_set_*` are the ids of every Capability the Policy governs before and after.
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
    """What a Compliance Officer is shown before approving a capability merge (AC-BI-005)."""

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
    """Per-class counts the guarded writer statement pins (A1 guard)."""

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
    """Pure result of planning one capability merge: preview, guard counts, snapshots, digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preview: CapabilityMergePreview
    expected: ExpectedCounts
    before: GraphSnapshot
    after: GraphSnapshot
    state_digest: str


class RoleRef(BaseModel):
    """A Role by id and name, as the bearer of an Obligation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str


class ObligationNodeState(BaseModel):
    """An Obligation as read for a merge: id, text and its other scalar properties."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    text: str
    properties: dict[str, PropertyValue]


class ObligationMergeState(BaseModel):
    """Everything the obligation-merge planner needs, read from the graph.

    `edges` holds every `SATISFIED_BY` (Requirement -> Obligation) and `REQUIRES`
    (Obligation -> Capability) edge incident to either Obligation. The Role of each side
    is its inbound `HAS` source(s); a well-formed Obligation has exactly one. A side that
    does not exist is `None`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    survivor: ObligationNodeState | None
    absorbed: ObligationNodeState | None
    survivor_roles: tuple[RoleRef, ...]
    absorbed_roles: tuple[RoleRef, ...]
    edges: tuple[EdgeRecord, ...]
    absorbed_requirement_refs: tuple[RequirementSourceRef, ...] = ()


class ObligationEdgesToMove(BaseModel):
    """Endpoint ids that union onto the survivor: Requirement ids and Capability ids."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    satisfied_by: tuple[str, ...]
    requires: tuple[str, ...]


class ObligationMergePreview(BaseModel):
    """What a Compliance Officer is shown before approving an obligation merge (AC-BI-005)."""

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
    """Per-class counts of the absorbed Obligation's edges the guarded statement pins (A2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    satisfied: int
    requires: int


class ObligationMergePlan(BaseModel):
    """Pure result of planning one obligation merge: preview, guard counts, snapshots, digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preview: ObligationMergePreview
    expected: ObligationExpectedCounts
    before: GraphSnapshot
    after: GraphSnapshot
    state_digest: str


class ReleaseState(BaseModel):
    """What the reader found for `release-capability-governance`: one Capability and its policy.

    `capability` is `None` when the id does not exist. `policies` are the Policies the
    Capability is `GOVERNED_BY` (the model allows exactly one). `governed_set` holds the id of
    every Capability the governing Policy governs (empty when ungoverned).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: CapabilityNodeState | None
    policies: tuple[GoverningPolicy, ...]
    governed_set: tuple[str, ...] = ()


class ReleasePreview(BaseModel):
    """What a Compliance Officer is shown before approving a governance release (AC-BI-013)."""

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
    """The release preview, the before/after snapshots (AC-BI-021) and the state digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preview: ReleasePreview
    before: GraphSnapshot
    after: GraphSnapshot
    state_digest: str


class CapabilityUnmergeInputs(BaseModel):
    """What a capability merge's audit snapshot says to reverse (AC-BI-019), derived without I/O.

    `restore_*` are the endpoint ids of every edge the merge moved off the absorbed node;
    `remove_*` are the subset that were not already on the survivor before the merge (only
    those leave the survivor again). `restore_policy_id` is set when the absorbed node had a
    `GOVERNED_BY` edge (moved in case 2, deleted in case 3); `remove_policy_edge` is true only
    when it was moved. `snapshot_survivor_edges` are the survivor's in-scope incoming edges as
    the merge left them, so anything else on the survivor now was added since.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    merge_approval_id: str
    survivor_id: str
    absorbed_id: str
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
    """The live graph around a tombstone, as `unmerge` needs it (a missing node is `None`).

    `redirects` maps a Capability id to the ids its `MERGED_INTO` edges currently point at.
    `survivor_edges` are the survivor's current in-scope incoming edges. `existing_endpoints`
    maps each edge class to the snapshot endpoint ids that still exist.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    absorbed: CapabilityNodeState | None
    survivor: CapabilityNodeState | None
    redirects: dict[str, tuple[str, ...]]
    survivor_edges: tuple[EdgeRecord, ...]
    survivor_policy_ids: tuple[str, ...]
    existing_endpoints: dict[str, tuple[str, ...]]
    policy_exists: bool = True


class UnmergeEdgeSet(BaseModel):
    """The edges one side of an unmerge touches, as endpoint ids per class."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requires: tuple[str, ...]
    covers: tuple[str, ...]
    mitigated_by: tuple[str, ...]
    governed_by: str | None = None


class CapabilityUnmergePreview(BaseModel):
    """What a Compliance Officer is shown before approving a capability unmerge (AC-BI-019)."""

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
    """The exact parameters the guarded unmerge statement pins and applies (A5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    restore_requires_ids: tuple[str, ...]
    remove_requires_ids: tuple[str, ...]
    restore_covers_ids: tuple[str, ...]
    remove_covers_ids: tuple[str, ...]
    restore_mitigated_ids: tuple[str, ...]
    remove_mitigated_ids: tuple[str, ...]
    restore_policy_id: str | None
    remove_policy_edge: bool


class CapabilityUnmergePlan(BaseModel):
    """Pure result of planning one capability unmerge: preview, statement params, snapshots."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preview: CapabilityUnmergePreview
    write: CapabilityUnmergeWrite
    restored_edges: tuple[EdgeRecord, ...]
    before: GraphSnapshot
    after: GraphSnapshot
    state_digest: str
    inputs: CapabilityUnmergeInputs


class ObligationUnmergeInputs(BaseModel):
    """What an obligation merge's audit snapshot says to recreate (AC-BI-019), derived without I/O.

    `properties` are the absorbed Obligation's scalar properties (its `text` included, its id
    excluded), `satisfied_by_ids` the Requirements that satisfied it and `requires_ids` the
    Capabilities it required. `snapshot_survivor_edges` are the survivor's `SATISFIED_BY` and
    `REQUIRES` edges as the merge left them and `survivor_edges_before_merge` as they were
    before it, so edges added since and edges that may come from the merge can be told apart.
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

    `marker_targets` maps an Obligation id to the `merged_into` ids of its `MergedObligation`
    markers. `capability_statuses` holds the effective status of each snapshot Capability that
    still exists. `survivor_edges` are the survivor's current `SATISFIED_BY` / `REQUIRES` edges.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    absorbed_exists: bool
    survivor_exists: bool
    survivor_text: str
    marker_targets: dict[str, tuple[str, ...]]
    role_exists: bool
    existing_requirements: tuple[str, ...]
    capability_statuses: dict[str, str]
    survivor_edges: tuple[EdgeRecord, ...]


class ObligationUnmergeEdgeSet(BaseModel):
    """Endpoint ids of the edges an obligation unmerge recreates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    satisfied_by: tuple[str, ...]
    requires: tuple[str, ...]


class ObligationUnmergePreview(BaseModel):
    """What a Compliance Officer is shown before approving an obligation unmerge (AC-BI-019)."""

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
    """The exact parameters the guarded obligation-unmerge statement pins and applies (A6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role_id: str
    satisfied_by_ids: tuple[str, ...]
    requires_ids: tuple[str, ...]
    properties: dict[str, PropertyValue]


class ObligationUnmergePlan(BaseModel):
    """Pure result of planning one obligation unmerge: preview, statement params, snapshots."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preview: ObligationUnmergePreview
    write: ObligationUnmergeWrite
    restored_edges: tuple[EdgeRecord, ...]
    before: GraphSnapshot
    after: GraphSnapshot
    state_digest: str
    inputs: ObligationUnmergeInputs
