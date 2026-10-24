"""Read the state a split of an automatically resolved Capability is planned from.

A split is decided from two sources: the single-tenant graph (the `ResolvedCapability` marker,
its chain, the terminal Capability A with its Policy and incoming edges) and the
`{short}_baseline` graphs (which still hold X and the edges each instrument gave it). The preview,
the executor's re-plan and the write step's guard all read through `read_capability_split_state`,
so all three judge the same state. Every read is read-only and goes through `graph_reader` or
`baseline_reader`.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from ps_service.graph_cleanup.baseline_reader import (
    read_baseline_capabilities,
    read_baseline_incoming_edges,
    read_baseline_instrument_ids,
)
from ps_service.graph_cleanup.graph_reader import (
    read_capability_present,
    read_capability_split_markers,
    read_existing_sources,
    read_obligation_merge_markers,
    read_split_terminal,
)
from ps_service.graph_cleanup.merge_planner import EDGE_CLASSES
from ps_service.graph_cleanup.split_planner import (
    CapabilitySplitState,
    HeldEdges,
    SplitHolder,
    held_edges,
    map_obligation_sources,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.graph_cleanup.models import BaselineCapability

__all__ = ["read_capability_split_state", "read_held_by_baselines", "read_held_edges"]

_RESOLVED_LABEL = "ResolvedCapability"


def read_held_by_baselines(
    baselines: Sequence[tuple[str, GraphHandle]], ids: set[str]
) -> dict[str, list[tuple[str, BaselineCapability, tuple[str, ...]]]]:
    """Each id's holding baselines, in graph-name order.

    Args:
        baselines: `(graph name, read handle)` of every `{short}_baseline` graph.
        ids: The Capability ids to look up.

    Returns:
        Per id that some baseline holds: `(graph name, its node, its instrument ids)` per holding
        baseline, ordered by graph name.

    Raises:
        GraphCleanupPersistenceError: a baseline could not be read.
    """
    held: dict[str, list[tuple[str, BaselineCapability, tuple[str, ...]]]] = {}
    for name, baseline in sorted(baselines, key=lambda pair: pair[0]):
        nodes = read_baseline_capabilities(baseline, ids)
        if not nodes:
            continue
        instruments = read_baseline_instrument_ids(baseline)
        for capability_id, node in nodes.items():
            held.setdefault(capability_id, []).append((name, node, instruments))
    return held


def read_held_edges(
    graph: GraphHandle,
    baselines: Sequence[tuple[str, GraphHandle]],
    rel_type: str,
    targets: Sequence[tuple[str, tuple[str, ...]]],
) -> dict[str, HeldEdges]:
    """Each `(X, aliases of A)`'s `rel_type` sources held on X and on A, from every baseline.

    Obligation sources follow their `MergedObligation` chains; PracticeArea and RiskPath sources
    keep their id. The marker and existence reads are skipped when no baseline holds an edge.

    Args:
        graph: Handle on the single-tenant graph, read only.
        baselines: `(graph name, read handle)` of every `{short}_baseline` graph.
        rel_type: `REQUIRES`, `COVERS` or `MITIGATED_BY`.
        targets: Per resolved Capability X, the ids whose marker chain ends at its terminal A.

    Returns:
        Per X, the sources the baselines hold on X and on A, and those skipped as missing.

    Raises:
        GraphCleanupValidationError: an Obligation marker chain contains a cycle.
        GraphCleanupPersistenceError: a graph could not be read.
    """
    ids = {x_id for x_id, _aliases in targets} | {
        alias for _x_id, aliases in targets for alias in aliases
    }
    edges = [
        edge
        for _name, baseline in baselines
        for edge in read_baseline_incoming_edges(baseline, rel_type, ids)
    ]
    source_ids: dict[str, str] = {}
    existing: frozenset[str] = frozenset()
    if edges:
        sources = {source for source, _target in edges}
        source_ids = (
            map_obligation_sources(sources, read_obligation_merge_markers(graph))
            if rel_type == "REQUIRES"
            else {source: source for source in sources}
        )
        existing = read_existing_sources(graph, rel_type, set(source_ids.values()))
    return {
        x_id: held_edges(
            edges,
            x_id=x_id,
            aliases=aliases,
            source_ids=source_ids,
            existing_sources=existing,
        )
        for x_id, aliases in targets
    }


def read_capability_split_state(
    graph: GraphHandle, baselines: Sequence[tuple[str, GraphHandle]], x_id: str
) -> CapabilitySplitState:
    """Read everything the split planner needs about `x_id`.

    The terminal, baseline and edge reads run only when `x_id` is a `ResolvedCapability` with no
    `Capability` node; otherwise the planner refuses on the marker reads alone.

    Args:
        graph: Handle on the single-tenant graph, read only.
        baselines: `(graph name, read handle)` of every `{short}_baseline` graph, read only.
        x_id: Id asked to be split.

    Returns:
        The split state; `plan_capability_split` validates and plans it.

    Raises:
        GraphCleanupValidationError: a marker chain is corrupt.
        GraphCleanupPersistenceError: a graph could not be read.
    """
    markers = read_capability_split_markers(graph, x_id)
    x_is_capability = read_capability_present(graph, capability_id=x_id)
    state = CapabilitySplitState(
        x_id=x_id,
        x_marker_label=markers.x_marker_label,
        marker_resolves_to=markers.marker_resolves_to,
        chain=markers.chain,
        x_is_capability=x_is_capability,
        terminal=None,
        terminal_policies=(),
        terminal_edges=(),
        holders=(),
        held={},
    )
    if x_is_capability or markers.x_marker_label != _RESOLVED_LABEL:
        return state
    terminal = read_split_terminal(graph, markers.chain[-1])
    if terminal.node is None:
        return replace(state, terminal_policies=terminal.policies)
    holders = tuple(
        SplitHolder(graph_name=name, node=node, instruments=instruments)
        for name, node, instruments in read_held_by_baselines(baselines, {x_id}).get(x_id, [])
    )
    held = (
        {
            rel_type: read_held_edges(graph, baselines, rel_type, [(x_id, markers.a_aliases)])[x_id]
            for rel_type, _source_label in EDGE_CLASSES
        }
        if holders
        else {}
    )
    return replace(
        state,
        terminal=terminal.node,
        terminal_policies=terminal.policies,
        terminal_edges=terminal.edges,
        holders=holders,
        held=held,
    )
