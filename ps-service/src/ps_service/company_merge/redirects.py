"""Merge redirects for Company Merge: keep a cleanup-merged node from being re-minted.

A Compliance Officer cleanup merge deletes the absorbed node and leaves a
`Merged<Kind> {id, merged_into}` marker. Ids are content hashes, so a later live merge or offline
restore whose baseline regenerates the absorbed node would otherwise mint it again. Before any
persist, `resolve_redirects` drops such a node from the baseline and maps its id to the terminal
survivor; the caller folds that mapping into `canonical_id_by_incoming_id`, so the
endpoint-agnostic `persist_rewired_edges` unions the node's edges onto the survivor and re-MERGEs
the (idempotent) `HAS` edge.

This module is the one place that reads the markers, follows chains, and rejects cycles and
dangling terminals, for every mergeable kind listed in `_KINDS`. Ids of the kinds never collide
(`role_`, `obl_`, `cap_` prefixes), so one absorbed-id to terminal mapping carries all of them.

Read-only; every failure is raised before the caller's first write.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, cast

from ps_service.company_merge.errors import CompanyMergeValidationError
from ps_service.logging import emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.models import (
        BaselineGraph,
        BaselineNode,
        CanonicalResolution,
        ProvenanceEdge,
    )
    from ps_service.logging import LogEmitter

__all__ = [
    "MergeableKind",
    "RedirectResolution",
    "compose_with_resolutions",
    "follow_markers",
    "read_markers",
    "resolve_redirects",
]

MergeableKind = Literal["Capability", "Obligation", "Role"]


@dataclass(frozen=True, slots=True)
class RedirectResolution:
    """The outcome of resolving a baseline's merge markers.

    Attributes:
        baseline: The baseline without its absorbed nodes.
        mapping: Absorbed id to terminal survivor id, for every kind; empty when nothing is
            redirected.
        by_kind: The same entries split by kind, with an entry for every kind (empty when the
            kind redirected nothing).
    """

    baseline: BaselineGraph
    mapping: dict[str, str]
    by_kind: dict[MergeableKind, dict[str, str]]


@dataclass(frozen=True, slots=True)
class _KindSpec:
    """How one mergeable kind is found in a baseline and in the graph (static queries only)."""

    nodes: Callable[[BaselineGraph], tuple[BaselineNode, ...]]
    nodes_field: str
    marker_label: str
    marker_query: str
    existing_query: str


_KINDS: Mapping[MergeableKind, _KindSpec] = {
    "Capability": _KindSpec(
        nodes=lambda baseline: baseline.capability_nodes,
        nodes_field="capability_nodes",
        marker_label="MergedCapability",
        marker_query="MATCH (m:MergedCapability) RETURN m.id, m.merged_into",
        existing_query="MATCH (n:Capability) WHERE n.id IN $ids RETURN n.id",
    ),
    "Role": _KindSpec(
        nodes=lambda baseline: baseline.role_nodes,
        nodes_field="role_nodes",
        marker_label="MergedRole",
        marker_query="MATCH (m:MergedRole) RETURN m.id, m.merged_into",
        existing_query="MATCH (n:Role) WHERE n.id IN $ids RETURN n.id",
    ),
    "Obligation": _KindSpec(
        nodes=lambda baseline: baseline.obligation_nodes,
        nodes_field="obligation_nodes",
        marker_label="MergedObligation",
        marker_query="MATCH (m:MergedObligation) RETURN m.id, m.merged_into",
        existing_query="MATCH (n:Obligation) WHERE n.id IN $ids RETURN n.id",
    ),
}


_LIVE_DEFINES_READ = (
    "MATCH (:RegulatoryInstrument {id: $instrument_id})-[:DEFINES]->(n:Role) "
    "WHERE n.id IN $ids RETURN n.id"
)


def _read_kind_markers(graph: GraphHandle, kind: MergeableKind) -> dict[str, str]:
    """Run the kind's one marker read; a marker without a target is a corrupt graph."""
    spec = _KINDS[kind]
    rows = cast("list[list[object]]", graph.query(spec.marker_query).result_set)
    markers: dict[str, str] = {}
    for row in rows:
        absorbed_id, target = row[0], row[1]
        if not isinstance(absorbed_id, str) or not isinstance(target, str):
            message = f"{spec.marker_label} {absorbed_id!r} has no merged_into target"
            raise CompanyMergeValidationError(message)
        markers[absorbed_id] = target
    return markers


def read_markers(graph: GraphHandle, kind: MergeableKind) -> dict[str, str]:
    """Read every marker of `kind` as `{absorbed_id: merged_into}` (one hop, one read).

    Args:
        graph: Read handle on the single-tenant graph.
        kind: The mergeable kind whose markers are read.

    Returns:
        A dict from each absorbed id to the id it was merged into; empty when no marker exists.
        Chains are not followed here.

    Raises:
        CompanyMergeValidationError: a marker carries no `merged_into` target.
    """
    return _read_kind_markers(graph, kind)


def follow_markers(start_id: str, markers: Mapping[str, str], kind: MergeableKind) -> str:
    """Follow `start_id`'s marker chain to its terminal survivor.

    Args:
        start_id: The id to start from.
        markers: Absorbed id to merged-into id, as read by `read_markers`.
        kind: The mergeable kind of the chain, named in a cycle error.

    Returns:
        The last id of the chain; an id with no marker is its own terminal.

    Raises:
        CompanyMergeValidationError: the chain contains a cycle.
    """
    seen: set[str] = set()
    current = start_id
    while current in markers:
        if current in seen:
            message = f"{_KINDS[kind].marker_label} cycle detected at {kind.lower()} {current!r}"
            raise CompanyMergeValidationError(message)
        seen.add(current)
        current = markers[current]
    return current


def compose_with_resolutions(
    mapping: Mapping[str, str], resolutions: Sequence[CanonicalResolution]
) -> dict[str, str]:
    """Follow each redirect's terminal through the Capability dedup resolutions.

    The terminal of a redirected Capability may itself be a baseline Capability that dedup then
    converges onto another canonical id; the redirect must land there, not on the terminal.

    Args:
        mapping: Absorbed id to terminal survivor id, as returned by `resolve_redirects`.
        resolutions: The Capability dedup resolutions of the baseline the redirects left behind.

    Returns:
        `mapping` with every terminal replaced by its canonical id when dedup resolved it, and
        left as is otherwise (Obligation and Role terminals are never dedup keys).
    """
    canonical = {resolution.incoming_id: resolution.canonical_id for resolution in resolutions}
    return {absorbed: canonical.get(terminal, terminal) for absorbed, terminal in mapping.items()}


def _check_terminals(
    graph: GraphHandle,
    kind: MergeableKind,
    baseline_ids: set[str],
    terminals: Mapping[str, str],
) -> None:
    """Raise unless every terminal is a baseline node or exists in `graph` (one batched read)."""
    outside = sorted({t for t in terminals.values() if t not in baseline_ids})
    existing: set[str] = set()
    if outside:
        rows = cast(
            "list[list[object]]",
            graph.query(_KINDS[kind].existing_query, {"ids": outside}).result_set,
        )
        existing = {cast("str", row[0]) for row in rows}
    for absorbed_id, terminal in terminals.items():
        if terminal not in baseline_ids and terminal not in existing:
            message = (
                f"merged {kind.lower()} {absorbed_id!r} redirects to {terminal!r}, "
                "which does not exist"
            )
            raise CompanyMergeValidationError(message)


def _drop_absorbed(
    baseline: BaselineGraph, kind: MergeableKind, absorbed: Mapping[str, str]
) -> BaselineGraph:
    """Return `baseline` without the `kind` nodes whose ids are keys of `absorbed`."""
    spec = _KINDS[kind]
    kept = tuple(node for node in spec.nodes(baseline) if node.id not in absorbed)
    return replace(baseline, **{spec.nodes_field: kept})


def _retarget_defines(
    graph: GraphHandle, baseline: BaselineGraph, roles: Mapping[str, str]
) -> BaselineGraph:
    """Point an absorbed Role's `DEFINES` at its survivor unless the survivor already has one.

    A `DEFINES` edge is a provenance edge the rewiring step does not map, so the terminal is
    written into the edge here; `HAS` is a bare edge and is rewired by the caller's mapping.
    The write keys the edge by endpoints and sets `source_ref` last-wins, so a retargeted edge
    would overwrite the survivor's own citation. It is therefore kept only when neither the
    baseline nor the live graph holds the instrument's `DEFINES` to that survivor (one read, only
    when a Role was redirected). A survivor the live graph holds without a `DEFINES` keeps it.
    """
    if not roles:
        return baseline
    kept: list[ProvenanceEdge] = []
    moved: list[ProvenanceEdge] = []
    for edge in baseline.provenance_edges:
        if edge.relationship_type == "DEFINES" and edge.target_id in roles:
            moved.append(replace(edge, target_id=roles[edge.target_id]))
        else:
            kept.append(edge)
    rows = cast(
        "list[list[object]]",
        graph.query(
            _LIVE_DEFINES_READ,
            {
                "instrument_id": baseline.regulatory_instrument_id,
                "ids": sorted({edge.target_id for edge in moved}),
            },
        ).result_set,
    )
    held = {edge.target_id for edge in kept if edge.relationship_type == "DEFINES"}
    held |= {cast("str", row[0]) for row in rows}
    for edge in moved:
        if edge.target_id not in held:
            kept.append(edge)
            held.add(edge.target_id)
    return replace(baseline, provenance_edges=tuple(kept))


def _resolve_kind(
    graph: GraphHandle, baseline: BaselineGraph, kind: MergeableKind
) -> tuple[BaselineGraph, dict[str, str]]:
    """Resolve the markers of one kind against `baseline`; unchanged when none applies."""
    nodes = _KINDS[kind].nodes(baseline)
    if not nodes:
        return baseline, {}
    markers = _read_kind_markers(graph, kind)
    terminals = {
        node.id: follow_markers(node.id, markers, kind) for node in nodes if node.id in markers
    }
    if not terminals:
        return baseline, {}
    _check_terminals(graph, kind, {node.id for node in nodes}, terminals)
    return _drop_absorbed(baseline, kind, terminals), terminals


def _log_redirects(
    baseline: BaselineGraph, counts: Mapping[MergeableKind, int], emitter: LogEmitter | None
) -> None:
    """Emit the one `resolve_merge_redirects` entry for a resolution that redirected something."""
    emit_log_entry(
        component="company_merge",
        action="resolve_merge_redirects",
        entity_id=baseline.regulatory_instrument_id,
        outcome="succeeded",
        extra={
            "redirected_count": sum(counts.values()),
            **{f"redirected_{kind.lower()}s": count for kind, count in counts.items()},
        },
        emitter=emitter,
    )


def resolve_redirects(
    graph: GraphHandle,
    baseline: BaselineGraph,
    *,
    emitter: LogEmitter | None = None,
) -> RedirectResolution:
    """Drop every absorbed node from `baseline` and map each to its terminal survivor.

    A kind the baseline holds no node of issues no read at all.

    Args:
        graph: Read handle on the single-tenant graph.
        baseline: The baseline whose absorbed nodes are dropped.
        emitter: Log emitter, or `None` for the default.

    Returns:
        The rewritten baseline and the absorbed-id to terminal-survivor mapping (empty, with the
        baseline returned unchanged, when nothing is redirected).

    Raises:
        CompanyMergeValidationError: a marker chain has a cycle, a marker has no target, or a
            chain ends at a node that exists in neither the graph nor the baseline.

    Side Effects:
        Reads the markers of each present kind and, when a terminal survivor is outside the
        baseline, one more query for its existence; when a Role is redirected, one more read for
        the `DEFINES` edges the instrument already holds. Emits one `resolve_merge_redirects` log
        entry when at least one node is redirected. Writes nothing.
    """
    mapping: dict[str, str] = {}
    counts: dict[MergeableKind, int] = {}
    by_kind: dict[MergeableKind, dict[str, str]] = {}
    for kind in _KINDS:
        baseline, by_kind[kind] = _resolve_kind(graph, baseline, kind)
        mapping.update(by_kind[kind])
        counts[kind] = len(by_kind[kind])
    baseline = _retarget_defines(graph, baseline, by_kind["Role"])
    if mapping:
        _log_redirects(baseline, counts, emitter)
    return RedirectResolution(baseline=baseline, mapping=mapping, by_kind=by_kind)
