"""Document-scoped reconciliation of one internal-seed submission against its graphs.

A submission is always the complete document, so whatever an instrument's `{short}_native` or
`{short}_baseline` graph holds beyond it was left behind by an earlier submission (a rename mints
a new content-hashed id and leaves the old node). `prepare_group` reads the keys the graph holds,
through the submitter (this module opens no connection), and `retirement_primitives` derives from
them the deletes to append to the submission's upserts so the group commits or fails as one unit.

The delete set is derived from graph contents and the built upserts only, never from a field of
the submission. Only the labels and edge types the seed can carry are ever retired: a node or
edge of any other kind in these graphs belongs to another writer and is left alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, get_args

from pydantic import ValidationError

from ps_service.graph_gateway.models import (
    DeleteEdge,
    DeleteNode,
    NodeRef,
    StampEdgeIdentity,
    UpsertEdge,
    UpsertNode,
)
from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError
from ps_service.ingestion.adapters.internal_seed.models import EdgeType, NodeLabel
from ps_service.ingestion.graph_submission import read_persisted_or_raise, require_caught_up

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ps_service.graph_gateway.graph_reader import EdgeKey, NodeKey, PersistedElements
    from ps_service.graph_gateway.models import Primitive
    from ps_service.ingestion.graph_submission import GroupSubmitter

SEED_NODE_LABELS: frozenset[str] = frozenset(get_args(NodeLabel))
"""Every node label the seed can carry (the intake vocabulary's `Literal`)."""

SEED_EDGE_TYPES: frozenset[str] = frozenset(get_args(EdgeType))
"""Every edge type the seed can carry (the intake vocabulary's `Literal`)."""


@dataclass(frozen=True, slots=True)
class GroupPlan:
    """One graph's submission: upserts then retirements, and the log position they were read at.

    Attributes:
        primitives: The upserts followed by the deletes of everything the graph holds that the
            upserts no longer name; submitted as one group.
        log_position: The graph's mutation-log position when its contents were read; the
            submitter uses it to detect a concurrent write.
    """

    primitives: tuple[Primitive, ...]
    log_position: int


def edge_identity(edge_type: str, source_id: str, target_id: str) -> str:
    """Return the gateway identity of an edge: `{type}:{source}->{target}`.

    Same-type edges between the same two nodes share one identity, so they collapse to a single
    edge.

    Args:
        edge_type: The edge type name, for example `HAS`.
        source_id: The source node's canonical id.
        target_id: The target node's canonical id.

    Returns:
        The identity string; endpoint labels are not part of it.
    """
    return f"{edge_type}:{source_id}->{target_id}"


def prepare_group(
    submitter: GroupSubmitter, graph: str, upserts: tuple[Primitive, ...]
) -> GroupPlan:
    """Read `graph` (writing nothing) and return `upserts` followed by what they no longer hold.

    Args:
        submitter: The group submitter through which the graph's log position and contents are
            read; no connection is opened here.
        graph: The graph name, `{short}_native` or `{short}_baseline`.
        upserts: The submission's built upserts for this graph; may be empty.

    Returns:
        A `GroupPlan` whose `primitives` are `upserts` followed by the retirement deletes, and
        whose `log_position` is the position the graph was read at.

    Side Effects:
        Reads the graph's log position and contents through `submitter`; writes nothing.

    Raises:
        InternalSeedError: the graph is behind its log, could not be read, or holds a key the
            gateway cannot name. Nothing has been written.
    """
    require_caught_up(submitter, graph, InternalSeedError)
    persisted = read_persisted_or_raise(submitter, graph, InternalSeedError)
    return GroupPlan(
        primitives=(*upserts, *retirement_primitives(persisted, upserts)),
        log_position=persisted.log_position,
    )


def retirement_primitives(
    persisted: PersistedElements, upserts: Iterable[Primitive]
) -> tuple[Primitive, ...]:
    """Return the deletes for what `persisted` holds and `upserts` no longer name, sorted.

    Only seed-vocabulary nodes and edges are retired (`SEED_NODE_LABELS`, `SEED_EDGE_TYPES`).

    Order: first a `DeleteEdge` for every seed-vocabulary edge the submission does not contain
    (edges incident to a retired node and edges dropped between survivors; an identity-less one
    as a stamp then a delete), then a `DeleteNode` per retired node. The edge deletes must come
    first: a node delete removes its edges, and an edge delete after it would be dropped from
    the log as a no-op.

    Args:
        persisted: The elements the graph currently holds, as read through the submitter.
        upserts: The submission's built upserts; anything they name is kept.

    Returns:
        The delete primitives in the order above; empty when nothing is left behind.

    Raises:
        InternalSeedError: a persisted key is one the gateway cannot name.
    """
    submitted = tuple(upserts)
    submitted_nodes: set[NodeKey] = {
        (primitive.label, primitive.id)
        for primitive in submitted
        if isinstance(primitive, UpsertNode)
    }
    submitted_edges: set[EdgeKey] = {
        (
            primitive.type,
            primitive.source.label,
            primitive.source.id,
            primitive.target.label,
            primitive.target.id,
            primitive.identity,
        )
        for primitive in submitted
        if isinstance(primitive, UpsertEdge)
    }
    gone_nodes = sorted(
        key for key in persisted.nodes if key[0] in SEED_NODE_LABELS and key not in submitted_nodes
    )
    gone_edges = sorted(
        key
        for key in persisted.edges
        if key[0] in SEED_EDGE_TYPES
        and key[1] in SEED_NODE_LABELS
        and key[3] in SEED_NODE_LABELS
        and key not in submitted_edges
    )
    submitted_shapes = {key[:5] for key in submitted_edges}
    gone_legacy = sorted(
        key
        for key in persisted.legacy_edges
        if key[0] in SEED_EDGE_TYPES
        and key[1] in SEED_NODE_LABELS
        and key[3] in SEED_NODE_LABELS
        and key not in submitted_shapes
    )
    try:
        return (
            *(p for key in gone_legacy for p in _stamp_then_delete(key)),
            *(_delete_edge(key) for key in gone_edges),
            *(DeleteNode(label=label, id=node_id) for label, node_id in gone_nodes),
        )
    except ValidationError as exc:
        message = "a persisted graph element has a key the graph gateway cannot name"
        raise InternalSeedError(message) from exc


def _delete_edge(key: EdgeKey) -> DeleteEdge:
    """Build the `DeleteEdge` that names the persisted edge `key`."""
    edge_type, source_label, source_id, target_label, target_id, identity = key
    return DeleteEdge(
        type=edge_type,
        identity=identity,
        source=NodeRef(label=source_label, id=source_id),
        target=NodeRef(label=target_label, id=target_id),
    )


def _stamp_then_delete(key: tuple[str, str, str, str, str]) -> tuple[Primitive, Primitive]:
    """Stamp an identity-less edge with its identity, then delete it by that identity.

    A delete can only name an edge that has an identity.
    """
    edge_type, source_label, source_id, target_label, target_id = key
    identity = edge_identity(edge_type, source_id, target_id)
    source = NodeRef(label=source_label, id=source_id)
    target = NodeRef(label=target_label, id=target_id)
    return (
        StampEdgeIdentity(type=edge_type, identity=identity, source=source, target=target),
        DeleteEdge(type=edge_type, identity=identity, source=source, target=target),
    )
