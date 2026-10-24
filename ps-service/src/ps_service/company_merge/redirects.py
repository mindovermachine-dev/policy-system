"""Redirects for Company Merge: keep a merged or resolved node from being minted or rescored.

Two markers redirect an id:

- A Compliance Officer cleanup merge deletes the absorbed node and leaves a
  `Merged<Kind> {id, merged_into}` marker (every mergeable kind).
- A semantic Capability resolution never mints the incoming id and leaves a
  `ResolvedCapability {id, resolves_to}` marker (Capability only).

Ids are content hashes, so a later live merge or offline restore whose baseline regenerates such
an id would otherwise mint it again, or score it again against a candidate pool that has grown
since. Before any dedup or persist, `resolve_redirects` drops such a node from the baseline and
maps its id to the terminal of its marker chain; the caller folds that mapping into
`canonical_id_by_incoming_id`, so the endpoint-agnostic `persist_rewired_edges` unions the node's
edges onto the terminal and re-MERGEs the (idempotent) `HAS` edge. A Capability chain may mix both
markers: a resolution onto a Capability a cleanup merge later absorbed follows on to the survivor.

This module is the one place that reads the markers, follows chains, and rejects cycles, dangling
terminals and an id held by two markers, for every mergeable kind listed in `_KINDS`. Ids of the
kinds never collide (`role_`, `obl_`, `cap_` prefixes), so one absorbed-id to terminal mapping
carries all of them.

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
        first_hop: Redirected id to the label of the marker that holds it (the first hop of its
            chain), for every redirected id of every kind.
    """

    baseline: BaselineGraph
    mapping: dict[str, str]
    by_kind: dict[MergeableKind, dict[str, str]]
    first_hop: dict[str, str]


@dataclass(frozen=True, slots=True)
class _MarkerSource:
    """One marker label of a kind: its static read and the property naming its target."""

    label: str
    query: str
    target_property: str


@dataclass(frozen=True, slots=True)
class _KindSpec:
    """How one mergeable kind is found in a baseline and in the graph (static queries only)."""

    nodes: Callable[[BaselineGraph], tuple[BaselineNode, ...]]
    nodes_field: str
    markers: tuple[_MarkerSource, ...]
    existing_query: str


_KINDS: Mapping[MergeableKind, _KindSpec] = {
    "Capability": _KindSpec(
        nodes=lambda baseline: baseline.capability_nodes,
        nodes_field="capability_nodes",
        markers=(
            _MarkerSource(
                label="MergedCapability",
                query="MATCH (m:MergedCapability) RETURN m.id, m.merged_into",
                target_property="merged_into",
            ),
            _MarkerSource(
                label="ResolvedCapability",
                query="MATCH (m:ResolvedCapability) RETURN m.id, m.resolves_to",
                target_property="resolves_to",
            ),
        ),
        existing_query="MATCH (n:Capability) WHERE n.id IN $ids RETURN n.id",
    ),
    "Role": _KindSpec(
        nodes=lambda baseline: baseline.role_nodes,
        nodes_field="role_nodes",
        markers=(
            _MarkerSource(
                label="MergedRole",
                query="MATCH (m:MergedRole) RETURN m.id, m.merged_into",
                target_property="merged_into",
            ),
        ),
        existing_query="MATCH (n:Role) WHERE n.id IN $ids RETURN n.id",
    ),
    "Obligation": _KindSpec(
        nodes=lambda baseline: baseline.obligation_nodes,
        nodes_field="obligation_nodes",
        markers=(
            _MarkerSource(
                label="MergedObligation",
                query="MATCH (m:MergedObligation) RETURN m.id, m.merged_into",
                target_property="merged_into",
            ),
        ),
        existing_query="MATCH (n:Obligation) WHERE n.id IN $ids RETURN n.id",
    ),
}


_LIVE_DEFINES_READ = (
    "MATCH (:RegulatoryInstrument {id: $instrument_id})-[:DEFINES]->(n:Role) "
    "WHERE n.id IN $ids RETURN n.id"
)


def _read_kind_markers(
    graph: GraphHandle, kind: MergeableKind
) -> tuple[dict[str, str], dict[str, str]]:
    """Run one read per marker label of the kind; a marker without a target is a corrupt graph.

    An id held by more than one marker is a corrupt graph too: no marker may silently win over
    another, so the check covers every marker read, whether or not the baseline holds the id.

    Returns the `{id: target}` map of every marker of the kind and the `{id: marker label}` map
    of the label that holds each id.
    """
    targets: dict[str, str] = {}
    labels: dict[str, str] = {}
    for source in _KINDS[kind].markers:
        rows = cast("list[list[object]]", graph.query(source.query).result_set)
        for row in rows:
            marked_id, target = row[0], row[1]
            if not isinstance(marked_id, str) or not isinstance(target, str):
                message = f"{source.label} {marked_id!r} has no {source.target_property} target"
                raise CompanyMergeValidationError(message)
            if marked_id in labels:
                message = f"{kind.lower()} {marked_id!r} is held by more than one marker label"
                raise CompanyMergeValidationError(message)
            targets[marked_id] = target
            labels[marked_id] = source.label
    return targets, labels


def read_markers(graph: GraphHandle, kind: MergeableKind) -> dict[str, str]:
    """Read every marker of `kind` as `{marked_id: target}` (one hop, one read per marker label).

    A Capability has two marker labels, `MergedCapability` (`merged_into`) and
    `ResolvedCapability` (`resolves_to`); both land in the one map, so a chain may mix them.

    Args:
        graph: Read handle on the single-tenant graph.
        kind: The mergeable kind whose markers are read.

    Returns:
        A dict from each marked id to the id its marker names; empty when no marker exists.
        Chains are not followed here.

    Raises:
        CompanyMergeValidationError: a marker carries no target, or one id is held by more than
            one marker.
    """
    return _read_kind_markers(graph, kind)[0]


def follow_markers(start_id: str, markers: Mapping[str, str], kind: MergeableKind) -> str:
    """Follow `start_id`'s marker chain to its terminal survivor.

    Args:
        start_id: The id to start from.
        markers: Marked id to target id, as read by `read_markers`.
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
            message = f"{kind.lower()} marker cycle detected at {kind.lower()} {current!r}"
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
                f"{kind.lower()} {absorbed_id!r} resolves to {terminal!r}, which does not exist"
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
) -> tuple[BaselineGraph, dict[str, str], dict[str, str]]:
    """Resolve the markers of one kind against `baseline`; unchanged when none applies.

    Returns the rewritten baseline, the redirected-id to terminal map and the redirected-id to
    first-hop marker label map.
    """
    nodes = _KINDS[kind].nodes(baseline)
    if not nodes:
        return baseline, {}, {}
    markers, labels = _read_kind_markers(graph, kind)
    terminals = {
        node.id: follow_markers(node.id, markers, kind) for node in nodes if node.id in markers
    }
    if not terminals:
        return baseline, {}, {}
    _check_terminals(graph, kind, {node.id for node in nodes}, terminals)
    first_hop = {marked_id: labels[marked_id] for marked_id in terminals}
    return _drop_absorbed(baseline, kind, terminals), terminals, first_hop


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
    """Drop every merged or resolved node from `baseline` and map each to its chain's terminal.

    A kind the baseline holds no node of issues no read at all.

    Args:
        graph: Read handle on the single-tenant graph.
        baseline: The baseline whose absorbed nodes are dropped.
        emitter: Log emitter, or `None` for the default.

    Returns:
        The rewritten baseline, the redirected-id to terminal mapping (empty, with the baseline
        returned unchanged, when nothing is redirected) and the label of the marker each
        redirected id was found under.

    Raises:
        CompanyMergeValidationError: a marker chain has a cycle, a marker has no target, one id
            is held by more than one marker, or a chain ends at a node that exists in neither the
            graph nor the baseline. Each message names ids only.

    Side Effects:
        Reads the markers of each present kind (one read per marker label) and, when a terminal
        is outside the baseline, one more query for its existence; when a Role is redirected, one
        more read for the `DEFINES` edges the instrument already holds. Emits one
        `resolve_merge_redirects` log entry when at least one node is redirected. Writes nothing.
    """
    mapping: dict[str, str] = {}
    counts: dict[MergeableKind, int] = {}
    by_kind: dict[MergeableKind, dict[str, str]] = {}
    first_hop: dict[str, str] = {}
    for kind in _KINDS:
        baseline, by_kind[kind], kind_first_hop = _resolve_kind(graph, baseline, kind)
        mapping.update(by_kind[kind])
        first_hop.update(kind_first_hop)
        counts[kind] = len(by_kind[kind])
    baseline = _retarget_defines(graph, baseline, by_kind["Role"])
    if mapping:
        _log_redirects(baseline, counts, emitter)
    return RedirectResolution(
        baseline=baseline, mapping=mapping, by_kind=by_kind, first_hop=first_hop
    )
