"""Pure planning and conflict detection for `unmerge-entities` of a Role merge.

No I/O. `derive_role_unmerge_inputs` reads what a `role.merge` audit row says to reverse;
`plan_role_unmerge` compares it with the live graph, rejects every conflict with an explanation and
returns the preview, the exact parameters the guarded writer pins, the before/after snapshots and a
state digest binding the approval.

Exactly the Obligations the merge moved go back to the recreated Role, and each leaves the survivor.
An Obligation the survivor gained after the merge cannot be told apart from its own (an Obligation
id embeds the hash of the Role that extracted it), so those stay on the survivor and are only
counted in the preview.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.models import (
    GraphSnapshot,
    NodeRecord,
    RoleUnmergeInputs,
    RoleUnmergePlan,
    RoleUnmergePreview,
    RoleUnmergeState,
    RoleUnmergeWrite,
)
from ps_service.graph_cleanup.role_planner import (
    defines_edge,
    has_edge,
    snapshot_role_node,
    sorted_edges,
)

if TYPE_CHECKING:
    from ps_service.graph_cleanup.audit_actions import RoleMergeDetails
    from ps_service.graph_cleanup.models import RoleState

__all__ = ["derive_role_unmerge_inputs", "plan_role_unmerge"]

_NOTE = (
    "Only the obligations the merge moved go back to the recreated role. Obligations the survivor "
    "gained since the merge stay on the survivor: their ids embed the hash of the role that "
    "extracted them, so they cannot be told apart from the survivor's own."
)
_UNREADABLE = "the merge's audit record does not hold what is needed, so it cannot be reversed"


def derive_role_unmerge_inputs(details: RoleMergeDetails) -> RoleUnmergeInputs:
    """Derive the Role and edges to recreate from a Role merge's audit row.

    Args:
        details: The `role.merge` audit details; both snapshots, the moved Obligations, the
            survivor's Obligation count after the merge and the instrument must be present, and
            `before` must hold the deleted Role.

    Returns:
        The absorbed Role's properties, its instrument and `DEFINES` `source_ref`, the moved
        Obligations and the survivor's count after the merge.

    Raises:
        GraphCleanupValidationError: the audit record lacks a snapshot, the moved set, the count,
            the instrument or the absorbed Role.
    """
    if (
        details.before is None
        or details.after is None
        or details.moved_obligation_ids is None
        or details.survivor_has_count_after_merge is None
        or not details.instrument_id
    ):
        raise GraphCleanupValidationError(_UNREADABLE)
    node = next(
        (n for n in details.before.nodes if n.label == "Role" and n.id == details.absorbed_id),
        None,
    )
    if node is None or not node.properties.get("name"):
        message = (
            "the merge's audit record does not hold the deleted role, so it cannot be reversed"
        )
        raise GraphCleanupValidationError(message)
    return RoleUnmergeInputs(
        merge_approval_id=details.approval_id,
        survivor_id=details.survivor_id,
        absorbed_id=details.absorbed_id,
        instrument_id=details.instrument_id,
        absorbed_properties=dict(node.properties),
        defines_source_ref=details.defines_source_ref,
        moved_obligation_ids=tuple(sorted(details.moved_obligation_ids)),
        survivor_has_count_after_merge=details.survivor_has_count_after_merge,
    )


def _check_marker(inputs: RoleUnmergeInputs, state: RoleUnmergeState) -> None:
    """Raise unless the Role is gone and its `MergedRole` marker still names the survivor."""
    absorbed_id, survivor_id = inputs.absorbed_id, inputs.survivor_id
    if state.absorbed_exists:
        message = f"role {absorbed_id!r} already exists again; there is nothing to unmerge"
        raise GraphCleanupValidationError(message)
    targets = state.marker_targets.get(absorbed_id, ())
    if survivor_id in targets:
        return
    if targets:
        message = (
            f"role {absorbed_id!r} was merged into {survivor_id!r}, but its MergedRole marker now "
            f"points at {', '.join(repr(t) for t in targets)}, so reversing the original merge "
            "would conflict"
        )
    else:
        message = (
            f"role {absorbed_id!r} has no MergedRole marker, so it is not a merged role that "
            "can be unmerged"
        )
    raise GraphCleanupValidationError(message)


def _check_moved_obligations(inputs: RoleUnmergeInputs, state: RoleUnmergeState) -> None:
    """Raise unless every moved Obligation exists and only the survivor bears it."""
    for obligation_id in inputs.moved_obligation_ids:
        bearers = state.moved_bearers.get(obligation_id, ())
        if not bearers:
            message = (
                f"obligation {obligation_id!r} moved by the merge no longer exists or no role "
                "bears it, so it cannot go back"
            )
            raise GraphCleanupValidationError(message)
        others = [role_id for role_id in bearers if role_id != inputs.survivor_id]
        if others or inputs.survivor_id not in bearers:
            message = (
                f"obligation {obligation_id!r} moved by the merge is now borne by "
                f"{', '.join(repr(r) for r in others)}, not by the survivor alone, so it cannot "
                "go back"
            )
            raise GraphCleanupValidationError(message)


def _check_state(inputs: RoleUnmergeInputs, state: RoleUnmergeState) -> RoleState:
    """Raise unless the Role can be recreated; return the survivor."""
    _check_marker(inputs, state)
    survivor = state.survivor
    if survivor is None:
        moved_to = state.marker_targets.get(inputs.survivor_id, ())
        where = f" (it was merged into {', '.join(repr(t) for t in moved_to)})" if moved_to else ""
        message = (
            f"the survivor role {inputs.survivor_id!r} no longer exists{where}, "
            "so the merge cannot be reversed"
        )
        raise GraphCleanupValidationError(message)
    if not state.instrument_exists:
        message = f"the instrument {inputs.instrument_id!r} that defined the role no longer exists"
        raise GraphCleanupValidationError(message)
    _check_moved_obligations(inputs, state)
    return survivor


def _digest(
    before: GraphSnapshot, inputs: RoleUnmergeInputs, write: RoleUnmergeWrite, added: int
) -> str:
    """Return the SHA-256 hex digest binding the before snapshot, merge approval and write."""
    canonical = json.dumps(
        {
            "before": before.model_dump(mode="json"),
            "merge_approval_id": inputs.merge_approval_id,
            "write": write.model_dump(mode="json"),
            "survivor_obligations_added_since_merge": added,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_role_unmerge(inputs: RoleUnmergeInputs, state: RoleUnmergeState) -> RoleUnmergePlan:
    """Plan recreating a merged-away Role under its original id against the live `state`.

    Args:
        inputs: What the merge's audit row says to recreate.
        state: The live graph around the marker.

    Returns:
        The plan: preview, write parameters, before/after snapshots and the state digest.

    Raises:
        GraphCleanupValidationError: the live graph no longer fits the merge, with the reason.
    """
    survivor = _check_state(inputs, state)
    moved = inputs.moved_obligation_ids
    added = max(0, len(state.survivor_obligation_ids) - inputs.survivor_has_count_after_merge)
    recreated = NodeRecord(
        label="Role", id=inputs.absorbed_id, properties=dict(inputs.absorbed_properties)
    )
    before = GraphSnapshot(
        nodes=(
            snapshot_role_node(survivor),
            NodeRecord(
                label="MergedRole",
                id=inputs.absorbed_id,
                properties={"merged_into": inputs.survivor_id},
            ),
        ),
        edges=sorted_edges(
            [has_edge(inputs.survivor_id, obligation_id) for obligation_id in moved]
        ),
    )
    after = GraphSnapshot(
        nodes=(snapshot_role_node(survivor), recreated),
        edges=sorted_edges(
            [
                defines_edge(inputs.instrument_id, inputs.absorbed_id),
                *(has_edge(inputs.absorbed_id, obligation_id) for obligation_id in moved),
            ]
        ),
    )
    write = RoleUnmergeWrite(
        absorbed_properties=dict(inputs.absorbed_properties),
        instrument_id=inputs.instrument_id,
        defines_source_ref=inputs.defines_source_ref,
        moved_obligation_ids=moved,
    )
    digest = _digest(before, inputs, write, added)
    return RoleUnmergePlan(
        preview=RoleUnmergePreview(
            merged_id=inputs.absorbed_id,
            merged_name=str(inputs.absorbed_properties["name"]),
            survivor_id=survivor.id,
            survivor_name=survivor.name,
            instrument_id=inputs.instrument_id,
            merge_approval_id=inputs.merge_approval_id,
            obligations_to_restore=moved,
            defines_source_ref=inputs.defines_source_ref,
            survivor_obligations_added_since_merge=added,
            note=_NOTE,
            state_digest=digest,
        ),
        write=write,
        before=before,
        after=after,
        state_digest=digest,
        inputs=inputs,
    )
