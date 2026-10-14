"""Facts derived from `DOMAIN_SCHEMA` so callers never hand-copy them.

Lookups of a property, its patchable fields and enum values by node label and property name.
Each takes an optional `schema` argument (default `DOMAIN_SCHEMA`) so tests can pass a variant.
"""

from __future__ import annotations

from ps_service.domain_schema.definition import DOMAIN_SCHEMA
from ps_service.domain_schema.errors import DomainSchemaError
from ps_service.domain_schema.model import EnumType, Node, Property, Schema


def _node_named(label: str, schema: Schema) -> Node:
    """Return the node with `label`, or raise `DomainSchemaError` if the schema has none."""
    for node in schema.nodes:
        if node.label == label:
            return node
    raise DomainSchemaError(f"unknown label {label!r}")


def property_named(label: str, name: str, schema: Schema = DOMAIN_SCHEMA) -> Property:
    """Return property `name` of node `label`.

    Args:
        label: A node label of `schema`, for example `Policy`.
        name: A property name declared on that node.
        schema: The schema to search; defaults to `DOMAIN_SCHEMA`.

    Returns:
        The matching `Property`.

    Raises:
        DomainSchemaError: `label` or `name` is not declared in `schema`.
    """
    for prop in _node_named(label, schema).properties:
        if prop.name == name:
            return prop
    raise DomainSchemaError(f"unknown property {name!r} on label {label!r}")


def patchable_fields(label: str, schema: Schema = DOMAIN_SCHEMA) -> frozenset[str]:
    """Return the names of `label`'s properties flagged neither identity nor lifecycle.

    These are the properties a caller may change in place without altering the node's identity or
    its lifecycle state.

    Args:
        label: A node label of `schema`.
        schema: The schema to search; defaults to `DOMAIN_SCHEMA`.

    Returns:
        The patchable property names; empty when every property is identity-bearing or
        lifecycle-managed.

    Raises:
        DomainSchemaError: `label` is not declared in `schema`.
    """
    return frozenset(
        prop.name
        for prop in _node_named(label, schema).properties
        if not (prop.is_identity_bearing or prop.is_lifecycle_managed)
    )


def enum_values(label: str, name: str, schema: Schema = DOMAIN_SCHEMA) -> tuple[str, ...]:
    """Return an enum property's values in schema order.

    Args:
        label: A node label of `schema`.
        name: The name of an enum-typed property on that node.
        schema: The schema to search; defaults to `DOMAIN_SCHEMA`.

    Returns:
        The allowed values, in declaration order.

    Raises:
        DomainSchemaError: `label` or `name` is unknown, or the property is not an enum.
    """
    prop = property_named(label, name, schema)
    if not isinstance(prop.type, EnumType):
        raise DomainSchemaError(f"property {name!r} on label {label!r} is not an enum")
    return prop.type.values
