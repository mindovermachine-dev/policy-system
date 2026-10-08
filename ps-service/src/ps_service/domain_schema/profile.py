"""Profiles: derive a narrower view of the schema by omitting, narrowing and adding properties.

A profile never mutates the base schema; `apply_profile` returns a new, re-validated
`Schema`, so renderers work on profiled schemas unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ps_service.domain_schema.errors import SchemaProfileError
from ps_service.domain_schema.model import (
    ConstType,
    EnumType,
    Node,
    Property,
    PropertyType,
    Schema,
    StringType,
)

if TYPE_CHECKING:
    from collections.abc import Callable

type EdgeKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class Omit:
    """Drop `property_name` from node `label`."""

    label: str
    property_name: str


@dataclass(frozen=True, slots=True)
class Narrow:
    """Replace the type of `label.property_name` with a stricter type `to`."""

    label: str
    property_name: str
    to: PropertyType


@dataclass(frozen=True, slots=True)
class Add:
    """Append `property` to node `label`."""

    label: str
    property: Property


type Operation = Omit | Narrow | Add


@dataclass(frozen=True, slots=True)
class Profile:
    """An ordered list of operations plus optional node/edge order and edge removals."""

    name: str
    operations: tuple[Operation, ...]
    node_order: tuple[str, ...] | None = None
    edge_order: tuple[EdgeKey, ...] | None = None
    omitted_edges: tuple[EdgeKey, ...] = ()


def _is_narrowing(base: PropertyType, to: PropertyType) -> bool:
    match base, to:
        case EnumType(values=base_values), EnumType(values=to_values):
            return set(to_values) <= set(base_values)
        case EnumType(values=base_values), ConstType(value=value):
            return value in base_values
        case StringType(min_length=base_min), StringType(min_length=to_min):
            return to_min >= base_min
        case _:
            return False


def _find(node: Node, property_name: str) -> Property:
    for prop in node.properties:
        if prop.name == property_name:
            return prop
    raise SchemaProfileError(f"{node.label} has no property {property_name!r}")


def _omit(node: Node, operation: Omit) -> Node:
    _find(node, operation.property_name)
    kept = tuple(p for p in node.properties if p.name != operation.property_name)
    return replace(node, properties=kept)


def _narrow(node: Node, operation: Narrow) -> Node:
    current = _find(node, operation.property_name)
    if not _is_narrowing(current.type, operation.to):
        raise SchemaProfileError(
            f"cannot narrow {node.label}.{operation.property_name} "
            f"from {current.type} to {operation.to}: rejected as not a refinement"
        )
    narrowed = tuple(
        replace(p, type=operation.to) if p.name == operation.property_name else p
        for p in node.properties
    )
    return replace(node, properties=narrowed)


def _add(node: Node, operation: Add) -> Node:
    if any(p.name == operation.property.name for p in node.properties):
        raise SchemaProfileError(f"{node.label} already has property {operation.property.name!r}")
    return replace(node, properties=(*node.properties, operation.property))


def _apply_operation(node: Node, operation: Operation) -> Node:
    match operation:
        case Omit():
            return _omit(node, operation)
        case Narrow():
            return _narrow(node, operation)
        case Add():
            return _add(node, operation)


def _reorder[T, K](
    items: tuple[T, ...], key: Callable[[T], K], order: tuple[K, ...] | None, what: str
) -> tuple[T, ...]:
    """Return `items` sorted by `order`; `order` must be an exact permutation of the keys."""
    if order is None:
        return items
    keys = [key(item) for item in items]
    if len(order) != len(keys) or set(order) != set(keys):
        raise SchemaProfileError(f"{what} order is not a permutation of the schema's {what}s")
    rank = {name: index for index, name in enumerate(order)}
    return tuple(sorted(items, key=lambda item: rank[key(item)]))


def apply_profile(schema: Schema, profile: Profile) -> Schema:
    """Return a new schema: `schema` with `profile` applied, validated like any `Schema`."""
    nodes = {node.label: node for node in schema.nodes}
    for operation in profile.operations:
        if operation.label not in nodes:
            raise SchemaProfileError(
                f"profile {profile.name!r} names unknown label {operation.label!r}"
            )
        nodes[operation.label] = _apply_operation(nodes[operation.label], operation)
    known = {edge.key for edge in schema.edges}
    for key in profile.omitted_edges:
        if key not in known:
            raise SchemaProfileError(f"profile {profile.name!r} omits unknown edge {key}")
    edges = tuple(edge for edge in schema.edges if edge.key not in profile.omitted_edges)
    ordered_nodes = _reorder(tuple(nodes.values()), lambda n: n.label, profile.node_order, "node")
    ordered_edges = _reorder(edges, lambda e: e.key, profile.edge_order, "edge")
    return Schema(nodes=ordered_nodes, edges=ordered_edges)
