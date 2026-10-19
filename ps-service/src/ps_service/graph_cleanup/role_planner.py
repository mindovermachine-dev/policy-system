"""Pure planning and validation for `merge-roles`.

No I/O: takes the `RoleMergeState` the reader produced and returns the preview, the sets the
writer pins, the before/after snapshots and a state digest. The digest binds a passkey approval to
the exact state that was previewed: both Roles, both `DEFINES` edges with their `source_ref` and
both `HAS` sets.

A Role is instrument-scoped, so both sides must be defined by the same RegulatoryInstrument. The
survivor keeps its `DEFINES` edge untouched; every Obligation the absorbed Role bears moves to the
survivor under the same id; the absorbed Role is deleted with its `DEFINES` edge and a
`MergedRole` marker records the redirect.
"""

from __future__ import annotations

import hashlib
import json

from ps_service.graph_cleanup.discovery import duplicate_text_basis
from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.models import (
    EdgeRecord,
    GraphSnapshot,
    NodeRecord,
    ObligationDuplicatePair,
    PendingReviewRef,
    RoleDefines,
    RoleExpectedCounts,
    RoleMergePlan,
    RoleMergePreview,
    RoleMergeState,
    RoleState,
)

__all__ = [
    "defines_edge",
    "has_edge",
    "plan_role_merge",
    "snapshot_role_node",
    "sorted_edges",
    "validate_role_merge",
    "validate_role_review",
]


def _present(state: RoleMergeState, side: str) -> RoleState:
    """Return the Role on `side` (`survivor` or `absorbed`), or raise when none has that id."""
    role = state.survivor if side == "survivor" else state.absorbed
    if role is not None:
        return role
    role_id = state.survivor_id if side == "survivor" else state.absorbed_id
    if role_id in state.marker_ids:
        message = (
            f"the {side} id {role_id!r} is a MergedRole marker, the trace of a Role that was "
            "already merged away, not a Role"
        )
        raise GraphCleanupValidationError(message)
    message = f"the {side} role does not exist"
    raise GraphCleanupValidationError(message)


def _instrument_of(defines: tuple[RoleDefines, ...], side: str, role_id: str) -> str:
    """Return the one instrument defining the Role, or raise when it has none or several."""
    if len(defines) != 1:
        message = (
            f"the {side} role {role_id!r} is not defined by exactly one instrument "
            f"(found {len(defines)})"
        )
        raise GraphCleanupValidationError(message)
    return defines[0].instrument_id


def _reject_edges_the_merge_cannot_move(state: RoleMergeState, absorbed: RoleState) -> None:
    """Raise when the absorbed Role has an unknown edge or a moved Obligation has another bearer."""
    expected_degree = len(state.absorbed_defines) + len(state.absorbed_obligation_ids)
    if state.absorbed_degree != expected_degree:
        message = (
            f"the absorbed role {absorbed.id!r} has edges other than DEFINES and HAS, which "
            "merging would delete without moving them"
        )
        raise GraphCleanupValidationError(message)
    if state.foreign_bearers:
        edge = min(state.foreign_bearers, key=lambda e: (e.target_id, e.source_id))
        message = (
            f"the obligation {edge.target_id!r} is also borne by the role {edge.source_id!r}; "
            f"every obligation to move must be borne by the absorbed role alone"
        )
        raise GraphCleanupValidationError(message)


def validate_role_merge(state: RoleMergeState) -> tuple[RoleState, RoleState, str]:
    """Reject a merge that must never get an approval; return (survivor, absorbed, instrument).

    Args:
        state: What the reader found for both Roles.

    Returns:
        The survivor, the absorbed Role and the id of the one instrument defining both.

    Raises:
        GraphCleanupValidationError: the same id twice, a side that is not a Role (a `MergedRole`
            marker is named as one), a side not defined by exactly one instrument, two Roles
            defined by different instruments, an absorbed Role with an edge other than `DEFINES`
            and `HAS`, or an Obligation to move that another Role also bears.
    """
    if state.survivor_id == state.absorbed_id:
        message = "a role cannot be merged into itself"
        raise GraphCleanupValidationError(message)
    survivor = _present(state, "survivor")
    absorbed = _present(state, "absorbed")
    survivor_instrument = _instrument_of(state.survivor_defines, "survivor", survivor.id)
    absorbed_instrument = _instrument_of(state.absorbed_defines, "absorbed", absorbed.id)
    if survivor_instrument != absorbed_instrument:
        message = (
            "roles defined by different instruments cannot be merged: the survivor is defined by "
            f"{survivor_instrument!r}, the absorbed by {absorbed_instrument!r}"
        )
        raise GraphCleanupValidationError(message)
    _reject_edges_the_merge_cannot_move(state, absorbed)
    return survivor, absorbed, survivor_instrument


def validate_role_review(
    review: PendingReviewRef | None, *, survivor_id: str, absorbed_id: str
) -> None:
    """Raise unless `review` is a Role review that names exactly the survivor and absorbed Roles.

    The order of the two ids in the review does not matter: the review records which Role arrived
    and which it was close to, the officer picks the survivor.

    Args:
        review: The review as read, None when no review has the id.
        survivor_id: Id of the Role that remains.
        absorbed_id: Id of the Role that is merged away.

    Raises:
        GraphCleanupValidationError: no such review (or already resolved), a review of another
            kind, or a review naming another pair.
    """
    if review is None:
        message = "the review does not exist or was already resolved"
        raise GraphCleanupValidationError(message)
    if review.kind != "Role":
        message = "the review is not a Role review"
        raise GraphCleanupValidationError(message)
    if {review.incoming_id, review.nearest_existing_id} != {survivor_id, absorbed_id}:
        message = "the review names a different pair of roles than the survivor and absorbed given"
        raise GraphCleanupValidationError(message)


def snapshot_role_node(role: RoleState) -> NodeRecord:
    """Return the snapshot record of a Role node, including its name."""
    return NodeRecord(label="Role", id=role.id, properties={"name": role.name, **role.properties})


def sorted_edges(edges: list[EdgeRecord]) -> tuple[EdgeRecord, ...]:
    """Order edges deterministically so equal states produce equal snapshots and digests."""
    return tuple(
        sorted(
            edges,
            key=lambda e: (e.rel_type, e.source_id, e.target_id, e.source_label, e.target_label),
        )
    )


def defines_edge(instrument_id: str, role_id: str) -> EdgeRecord:
    """Return the `DEFINES` edge from the instrument to the Role."""
    return EdgeRecord(
        rel_type="DEFINES",
        source_label="RegulatoryInstrument",
        source_id=instrument_id,
        target_label="Role",
        target_id=role_id,
    )


def has_edge(role_id: str, obligation_id: str) -> EdgeRecord:
    """Return the `HAS` edge from the Role to the Obligation."""
    return EdgeRecord(
        rel_type="HAS",
        source_label="Role",
        source_id=role_id,
        target_label="Obligation",
        target_id=obligation_id,
    )


def _duplicate_pairs(state: RoleMergeState) -> tuple[ObligationDuplicatePair, ...]:
    """Pair each moving Obligation with each surviving one whose text duplicates it.

    Uses the `find-duplicate-obligations` rule, so these are the pairs that tool reports for the
    survivor once the merge has run. Pairs inside one side are not the merge's doing and are left
    out.
    """
    texts = state.obligation_texts
    pairs: list[ObligationDuplicatePair] = []
    for survivor_obligation in sorted(state.survivor_obligation_ids):
        for absorbed_obligation in sorted(state.absorbed_obligation_ids):
            if survivor_obligation not in texts or absorbed_obligation not in texts:
                continue
            basis = duplicate_text_basis(texts[survivor_obligation], texts[absorbed_obligation])
            if basis is not None:
                pairs.append(
                    ObligationDuplicatePair(
                        survivor_obligation_id=survivor_obligation,
                        survivor_text=texts[survivor_obligation],
                        absorbed_obligation_id=absorbed_obligation,
                        absorbed_text=texts[absorbed_obligation],
                        basis=basis,
                    )
                )
    return tuple(pairs)


def _digest(
    before: GraphSnapshot, state: RoleMergeState, pairs: tuple[ObligationDuplicatePair, ...]
) -> str:
    """Return the SHA-256 hex digest of the previewed state.

    Covers the before snapshot, the `source_ref` of both `DEFINES` edges (a snapshot edge has no
    properties), the survivor's whole `HAS` set, which the snapshot holds only for moved
    Obligations, and the duplicate pairs the preview lists.
    """
    canonical = json.dumps(
        {
            "before": before.model_dump(mode="json"),
            "defines": sorted(
                (side, d.instrument_id, d.source_ref)
                for side, defines in (
                    ("survivor", state.survivor_defines),
                    ("absorbed", state.absorbed_defines),
                )
                for d in defines
            ),
            "survivor_has": sorted(state.survivor_obligation_ids),
            "duplicate_pairs": [pair.model_dump(mode="json") for pair in pairs],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_role_merge(state: RoleMergeState) -> RoleMergePlan:
    """Plan a Role merge from the read `state` (no I/O).

    Args:
        state: What the reader found for both Roles; validated by `validate_role_merge`.

    Returns:
        A `RoleMergePlan`: the preview, the sets the writer pins, the before/after snapshots and
        the state digest. The after snapshot keeps the survivor with the moved `HAS` edges and
        replaces the absorbed Role with a `MergedRole` marker.
    """
    survivor, absorbed, instrument_id = validate_role_merge(state)
    moved = tuple(sorted(state.absorbed_obligation_ids))
    survivor_kept = tuple(sorted(state.survivor_obligation_ids))
    before = GraphSnapshot(
        nodes=(snapshot_role_node(survivor), snapshot_role_node(absorbed)),
        edges=sorted_edges(
            [
                defines_edge(instrument_id, survivor.id),
                defines_edge(instrument_id, absorbed.id),
                *(has_edge(absorbed.id, obligation_id) for obligation_id in moved),
            ]
        ),
    )
    after = GraphSnapshot(
        nodes=(
            snapshot_role_node(survivor),
            NodeRecord(label="MergedRole", id=absorbed.id, properties={"merged_into": survivor.id}),
        ),
        edges=sorted_edges(
            [
                defines_edge(instrument_id, survivor.id),
                *(has_edge(survivor.id, obligation_id) for obligation_id in moved),
            ]
        ),
    )
    pairs = _duplicate_pairs(state)
    digest = _digest(before, state, pairs)
    return RoleMergePlan(
        preview=RoleMergePreview(
            survivor_id=survivor.id,
            survivor_name=survivor.name,
            absorbed_id=absorbed.id,
            absorbed_name=absorbed.name,
            instrument_id=instrument_id,
            moved_obligation_ids=moved,
            duplicate_obligation_pairs=pairs,
            state_digest=digest,
        ),
        expected=RoleExpectedCounts(
            absorbed_obligation_ids=moved, survivor_obligation_ids=survivor_kept
        ),
        before=before,
        after=after,
        state_digest=digest,
        absorbed_defines_source_ref=state.absorbed_defines[0].source_ref,
        survivor_has_count_after_merge=len(survivor_kept) + len(moved),
    )
