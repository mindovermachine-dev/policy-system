"""Pure planning for splitting an automatically resolved Capability back out of its terminal.

No I/O. A `ResolvedCapability` X has no node and no edges of its own in the single-tenant graph:
its edges were written onto its terminal Capability A. The `{short}_baseline` graphs still hold
X, so which edge belongs to X and which to A is decided from what the baselines hold:

- an edge is held on X when some baseline holds it with endpoint X;
- an edge is held on A when some baseline holds it with endpoint A or with any other id whose
  marker chain ends at A (an alias of A).

A baseline source id is first mapped to its single-tenant id. An Obligation follows its
`MergedObligation` chain to the survivor; a PracticeArea or RiskPath keeps its id. A source with no
node in the single-tenant graph is skipped and counted, never invented.

`plan_capability_split` validates the request, classifies each source's edge (moves, shared,
added, stays), builds the preview and the before/after snapshots, and digests the previewed state
so a passkey approval binds to exactly what the officer saw. `GOVERNED_BY` is never a candidate:
A keeps its Policy and X starts ungoverned.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.merge_planner import EDGE_CLASSES
from ps_service.graph_cleanup.models import (
    BaselineCapability,
    CapabilityNodeState,
    CapabilitySplitPlan,
    CapabilitySplitPreview,
    EdgeRecord,
    GoverningPolicy,
    GraphSnapshot,
    NodeRecord,
    PropertyValue,
    SplitEdges,
    SplitEdgeSet,
    SplitGovernance,
)
from ps_service.graph_cleanup.state_digest import canonical_digest

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping

__all__ = [
    "CapabilitySplitState",
    "HeldEdges",
    "SplitHolder",
    "held_edges",
    "map_obligation_sources",
    "plan_capability_split",
    "preview_policy_id",
    "split_digest_payload",
]

_RESOLVED_LABEL = "ResolvedCapability"
_MERGED_LABEL = "MergedCapability"
# Relationship type -> the key of its edge set in the preview and the digest.
_EDGE_KEYS = {"REQUIRES": "requires", "COVERS": "covers", "MITIGATED_BY": "mitigated_by"}


@dataclass(frozen=True, slots=True)
class HeldEdges:
    """The single-tenant sources of one edge type that the baselines hold on X and on A.

    Attributes:
        held_x: Existing single-tenant sources some baseline links to X.
        held_a: Single-tenant sources some baseline links to A or to an alias of A.
        skipped_missing: Mapped sources some baseline links to X that have no single-tenant
            node.
    """

    held_x: frozenset[str]
    held_a: frozenset[str]
    skipped_missing: frozenset[str]


def map_obligation_sources(ids: Iterable[str], markers: Mapping[str, str]) -> dict[str, str]:
    """Map baseline Obligation ids to their single-tenant ids through `MergedObligation` chains.

    Args:
        ids: Baseline Obligation ids.
        markers: `{absorbed id: merged_into}` of every `MergedObligation` marker.

    Returns:
        Each id to the terminal of its marker chain; an id with no marker maps to itself.

    Raises:
        GraphCleanupValidationError: a marker chain contains a cycle.
    """
    from ps_service.company_merge.errors import (  # noqa: PLC0415 -- function-local keeps graph_cleanup off Company Merge at import
        CompanyMergeValidationError,
    )
    from ps_service.company_merge.redirects import (  # noqa: PLC0415 -- function-local, as above
        follow_markers,
    )

    try:
        return {source: follow_markers(source, markers, "Obligation") for source in ids}
    except CompanyMergeValidationError as exc:
        raise GraphCleanupValidationError(str(exc)) from exc


def held_edges(
    baseline_edges: Iterable[tuple[str, str]],
    *,
    x_id: str,
    aliases: Collection[str],
    source_ids: Mapping[str, str],
    existing_sources: Collection[str],
) -> HeldEdges:
    """Split one edge type's baseline edges into the sources held on X and those held on A.

    Args:
        baseline_edges: `(source id, target id)` of every baseline edge of the type, across all
            baselines; a repeated edge counts once.
        x_id: The resolved Capability's id.
        aliases: A and every other id whose marker chain ends at A; X is never an alias.
        source_ids: Baseline source id to single-tenant id; an id not in it keeps its id.
        existing_sources: Single-tenant ids of the type's source label that exist.

    Returns:
        The sources held on X (existing only), those held on A, and the X-side sources skipped
        because no single-tenant node has their id. Edges to any other target are ignored.
    """
    a_side = set(aliases) - {x_id}
    existing = set(existing_sources)
    held_x: set[str] = set()
    held_a: set[str] = set()
    skipped: set[str] = set()
    for source, target in baseline_edges:
        mapped = source_ids.get(source, source)
        if target == x_id:
            (held_x if mapped in existing else skipped).add(mapped)
        elif target in a_side:
            held_a.add(mapped)
    return HeldEdges(
        held_x=frozenset(held_x), held_a=frozenset(held_a), skipped_missing=frozenset(skipped)
    )


@dataclass(frozen=True, slots=True)
class SplitHolder:
    """One `{short}_baseline` graph that holds X.

    Attributes:
        graph_name: The baseline graph's name.
        node: X as that baseline holds it.
        instruments: The `RegulatoryInstrument` ids the baseline holds, sorted.
    """

    graph_name: str
    node: BaselineCapability
    instruments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapabilitySplitState:
    """Everything the split planner needs, read from the policy graph and the baselines.

    Attributes:
        x_id: The id asked to be split.
        x_marker_label: The Capability marker label holding `x_id`; None when none holds it.
        marker_resolves_to: The id that marker names; None when no marker holds `x_id`.
        chain: Ids from X along its marker chain to the terminal A, both included.
        x_is_capability: True when a `Capability` node with `x_id` exists.
        terminal: A's node; None when the chain ends on no Capability.
        terminal_policies: The Policies governing A.
        terminal_edges: Every `REQUIRES` / `COVERS` / `MITIGATED_BY` edge into A.
        holders: The baselines that hold X, in graph-name order.
        held: Per relationship type, the sources the baselines hold on X and on A.
    """

    x_id: str
    x_marker_label: str | None
    marker_resolves_to: str | None
    chain: tuple[str, ...]
    x_is_capability: bool
    terminal: CapabilityNodeState | None
    terminal_policies: tuple[GoverningPolicy, ...]
    terminal_edges: tuple[EdgeRecord, ...]
    holders: tuple[SplitHolder, ...]
    held: Mapping[str, HeldEdges]


def _validate(state: CapabilitySplitState) -> tuple[CapabilityNodeState, str]:
    """Reject a split that must never get an approval; return A's node and the marker target."""
    x_id = state.x_id
    if state.x_is_capability:
        message = f"capability {x_id!r} already exists as a Capability; there is nothing to split"
        raise GraphCleanupValidationError(message)
    if state.x_marker_label == _MERGED_LABEL:
        message = (
            f"capability {x_id!r} was merged by a cleanup merge, not resolved automatically; "
            "reverse a cleanup merge with unmerge-entities"
        )
        raise GraphCleanupValidationError(message)
    if state.x_marker_label != _RESOLVED_LABEL or state.marker_resolves_to is None:
        message = f"there is no automatic resolution of capability {x_id!r}"
        raise GraphCleanupValidationError(message)
    if state.terminal is None:
        terminal_id = state.chain[-1] if state.chain else state.marker_resolves_to
        message = (
            f"capability {x_id!r} resolves to {terminal_id!r}, which is not an existing Capability"
        )
        raise GraphCleanupValidationError(message)
    if len(state.terminal_policies) > 1:
        message = "a capability has more than one governing policy; this is a data fault"
        raise GraphCleanupValidationError(message)
    if not state.holders:
        message = (
            f"no regulation baseline holds capability {x_id!r}, so it cannot be minted from one"
        )
        raise GraphCleanupValidationError(message)
    return state.terminal, state.marker_resolves_to


def _source_holder(holders: tuple[SplitHolder, ...]) -> SplitHolder:
    """The source baseline: the first holder with an embedding, else the first holder."""
    return next((h for h in holders if h.node.embedding is not None), holders[0])


def _current(state: CapabilitySplitState, rel_type: str, a_id: str) -> frozenset[str]:
    """Sources of the current `rel_type` edges into A."""
    return frozenset(
        e.source_id for e in state.terminal_edges if e.rel_type == rel_type and e.target_id == a_id
    )


def _held(state: CapabilitySplitState, rel_type: str) -> HeldEdges:
    """The held sources of one edge type; none when no baseline edge of it was read."""
    return state.held.get(rel_type) or HeldEdges(frozenset(), frozenset(), frozenset())


def split_digest_payload(state: CapabilitySplitState) -> dict[str, object]:
    """The canonical, JSON-native inputs of a split's state digest.

    Any change to the marker, its chain, A's status or governance, whether X exists as a
    Capability or a `MergedCapability`, the held and current edge sets, or the source baseline's X
    gives a different payload, so an approval signed for one state never applies to another.

    Args:
        state: The split state as read.

    Returns:
        The payload `canonical_digest` hashes; every list is sorted except `chain`, whose order is
        the chain's.
    """
    terminal = state.terminal
    a_id = state.chain[-1] if state.chain else None
    edges: dict[str, object] = {}
    for rel_type, _source_label in EDGE_CLASSES:
        held = _held(state, rel_type)
        edges[_EDGE_KEYS[rel_type]] = {
            "held_x": sorted(held.held_x),
            "held_a_and_x": sorted(held.held_a & held.held_x),
            "current_a": sorted(_current(state, rel_type, a_id)) if a_id is not None else [],
        }
    source = _source_holder(state.holders) if state.holders else None
    embedding = source.node.embedding if source is not None else None
    return {
        "x_id": state.x_id,
        "marker_resolves_to": state.marker_resolves_to,
        "chain": list(state.chain),
        "a_id": a_id,
        "a_status": terminal.status if terminal is not None else None,
        "a_governors": sorted([p.id, p.status] for p in state.terminal_policies),
        "x_is_capability": state.x_is_capability,
        "x_is_merged": state.x_marker_label == _MERGED_LABEL,
        "edges": edges,
        "source_baseline": source.graph_name if source is not None else None,
        "source_name": source.node.name if source is not None else None,
        "source_embedding_sha256": (
            hashlib.sha256(json.dumps(list(embedding)).encode("utf-8")).hexdigest()
            if embedding is not None
            else None
        ),
    }


def _edge(rel_type: str, source_label: str, source_id: str, target_id: str) -> EdgeRecord:
    return EdgeRecord(
        rel_type=rel_type,
        source_label=source_label,
        source_id=source_id,
        target_label="Capability",
        target_id=target_id,
    )


def _sorted_edges(edges: Iterable[EdgeRecord]) -> tuple[EdgeRecord, ...]:
    """Order edges deterministically so equal states produce equal snapshots."""
    return tuple(
        sorted(edges, key=lambda e: (e.rel_type, e.source_id, e.target_id, e.source_label))
    )


def _governed_by(capability_id: str, policy_id: str) -> EdgeRecord:
    return EdgeRecord(
        rel_type="GOVERNED_BY",
        source_label="Capability",
        source_id=capability_id,
        target_label="Policy",
        target_id=policy_id,
    )


def _acknowledgment_text(policy: GoverningPolicy, a_id: str, x_id: str, moving: int) -> str:
    """The text the approver must accept when A is governed."""
    return (
        f"Splitting changes what policy {policy.title!r} ({policy.id}) covers: capability "
        f"{a_id!r} stays governed by it, the split capability {x_id!r} starts with no governing "
        f"policy, and the {moving} obligation(s) whose REQUIRES edge moves to {x_id!r} are no "
        f"longer covered by it. The policy is `{policy.status}`: its content and version do not "
        "change."
    )


def _minted_node(x_id: str, node: BaselineCapability) -> NodeRecord:
    """The snapshot record of the minted X: the baseline's scalar properties, no embedding."""
    properties: dict[str, PropertyValue] = {"name": node.name}
    if node.confidence is not None:
        properties["confidence"] = node.confidence
    if node.description is not None:
        properties["description"] = node.description
    return NodeRecord(label="Capability", id=x_id, properties=properties)


def plan_capability_split(state: CapabilitySplitState) -> CapabilitySplitPlan:
    """Plan the split of resolved Capability X out of its terminal A from `state` (no I/O).

    Per edge type T: every source a baseline holds on X gets an edge on X; its edge on A is
    deleted (moves) unless a baseline also holds it on A (shared); a source with no edge on A
    today gains one on X only (added); every other edge of A is untouched (stays). X is minted
    from the source baseline's node and the `ResolvedCapability` marker is deleted.

    Args:
        state: The split state as read.

    Returns:
        The preview, the node X is minted from, the before/after snapshots and the state digest.

    Raises:
        GraphCleanupValidationError: X already exists as a Capability, X is a `MergedCapability`,
            there is no `ResolvedCapability` X, the chain ends on no Capability, A has more than
            one governing Policy, or no baseline holds X.
    """
    a_node, resolves_to = _validate(state)
    a_id = a_node.id
    x_id = state.x_id
    source = _source_holder(state.holders)
    sets: dict[str, SplitEdgeSet] = {}
    after_edges: list[EdgeRecord] = []
    skipped = 0
    for rel_type, source_label in EDGE_CLASSES:
        held = _held(state, rel_type)
        current = _current(state, rel_type, a_id)
        skipped += len(held.skipped_missing)
        moves = (held.held_x & current) - held.held_a
        sets[_EDGE_KEYS[rel_type]] = SplitEdgeSet(
            moves=tuple(sorted(moves)),
            shared=tuple(sorted(held.held_x & current & held.held_a)),
            added=tuple(sorted(held.held_x - current)),
            stays=tuple(sorted(current - held.held_x)),
        )
        after_edges.extend(_edge(rel_type, source_label, s, a_id) for s in current - moves)
        after_edges.extend(_edge(rel_type, source_label, s, x_id) for s in held.held_x)
    edges = SplitEdges(
        requires=sets["requires"], covers=sets["covers"], mitigated_by=sets["mitigated_by"]
    )
    policy = state.terminal_policies[0] if state.terminal_policies else None
    moving = len(edges.requires.moves)
    governance = (
        SplitGovernance(
            policy=policy,
            acknowledgment_required=True,
            acknowledgment_text=_acknowledgment_text(policy, a_id, x_id, moving),
        )
        if policy is not None
        else None
    )
    digest = canonical_digest(split_digest_payload(state))
    a_record = NodeRecord(
        label="Capability",
        id=a_id,
        properties={**a_node.properties, "name": a_node.name, "status": a_node.status},
    )
    policy_nodes = (
        (
            NodeRecord(
                label="Policy",
                id=policy.id,
                properties={"title": policy.title, "status": policy.status},
            ),
        )
        if policy is not None
        else ()
    )
    governed = [_governed_by(a_id, policy.id)] if policy is not None else []
    before = GraphSnapshot(
        nodes=(
            a_record,
            NodeRecord(label=_RESOLVED_LABEL, id=x_id, properties={"resolves_to": resolves_to}),
            *policy_nodes,
        ),
        edges=_sorted_edges([*state.terminal_edges, *governed]),
    )
    after = GraphSnapshot(
        nodes=(a_record, _minted_node(x_id, source.node), *policy_nodes),
        edges=_sorted_edges([*after_edges, *governed]),
    )
    preview = CapabilitySplitPreview(
        x_id=x_id,
        x_name=source.node.name,
        a_id=a_id,
        a_name=a_node.name,
        marker_resolves_to=resolves_to,
        source_baseline=source.graph_name,
        source_instruments=source.instruments,
        embedding_available=source.node.embedding is not None,
        edges=edges,
        governance=governance,
        obligations_moving=moving,
        skipped_missing_endpoints=skipped,
        state_digest=digest,
    )
    return CapabilitySplitPlan(
        preview=preview, minted=source.node, before=before, after=after, state_digest=digest
    )


def preview_policy_id(preview: Mapping[str, object]) -> str | None:
    """The id of A's governing Policy in a split preview dumped to JSON, if it names one.

    Args:
        preview: A `CapabilitySplitPreview` as `model_dump(mode="json")` gives it (the approval's
            display summary).

    Returns:
        The Policy id, or None when A is ungoverned or the summary holds no such field.
    """
    governance = preview.get("governance")
    if not isinstance(governance, dict):
        return None
    policy = cast("dict[str, object]", governance).get("policy")
    if not isinstance(policy, dict):
        return None
    policy_id = cast("dict[str, object]", policy).get("id")
    return policy_id if isinstance(policy_id, str) else None
