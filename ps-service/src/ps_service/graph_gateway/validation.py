"""Validation of a submitted group before anything is logged (issue #206).

Shape and value rules live in the models (the trust boundary); this module adds the rules that
need the gateway's own knowledge: the label allow-list and the caller's preconditions. Rules
that need the graph's current state (missing targets, no-ops) live in `noop_filter`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.graph_gateway.errors import StaleGraphStateError
from ps_service.graph_gateway.label_allow_list import (
    require_allowed_node_label,
    require_allowed_relationship_type,
)
from ps_service.graph_gateway.models import (
    DeleteEdge,
    DeleteNode,
    MergeProperty,
    RemoveProperty,
    UpsertEdge,
    UpsertNode,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from ps_service.graph_gateway.models import MutationGroup, Primitive


def validate_group(group: MutationGroup) -> None:
    """Raise `UnlistedNameError` if `group` names a label or type the gateway does not allow."""
    for primitive in group.primitives:
        for label in node_labels(primitive):
            require_allowed_node_label(label)
        for relationship_type in _relationship_types(primitive):
            require_allowed_relationship_type(relationship_type)


def require_preconditions(group: MutationGroup, last_position: Callable[[str], int]) -> None:
    """Raise `StaleGraphStateError` if any caller precondition of `group` does not hold.

    Reads the log position only (`last_position(graph)`); the graph is never consulted.
    """
    for precondition in group.preconditions:
        actual = last_position(group.graph)
        if actual != precondition.position:
            raise StaleGraphStateError(group.graph, precondition.position, actual)


def node_labels(primitive: Primitive) -> Iterator[str]:
    """Yield every node label `primitive` names (an edge names both endpoints' labels)."""
    match primitive:
        case UpsertNode() | MergeProperty() | RemoveProperty() | DeleteNode():
            yield primitive.label
        case UpsertEdge() | DeleteEdge():
            yield primitive.source.label
            yield primitive.target.label


def _relationship_types(primitive: Primitive) -> Iterator[str]:
    if isinstance(primitive, UpsertEdge | DeleteEdge):
        yield primitive.type
