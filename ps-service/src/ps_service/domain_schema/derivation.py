"""Facts derived from `DOMAIN_SCHEMA` so callers never hand-copy them (issue #199)."""

from __future__ import annotations

from ps_service.domain_schema.definition import DOMAIN_SCHEMA
from ps_service.domain_schema.errors import DomainSchemaError
from ps_service.domain_schema.model import EnumType, Node, Property, Schema


def _node_named(label: str, schema: Schema) -> Node:
    for node in schema.nodes:
        if node.label == label:
            return node
    raise DomainSchemaError(f"unknown label {label!r}")


def property_named(label: str, name: str, schema: Schema = DOMAIN_SCHEMA) -> Property:
    """Return property `name` of node `label`; raise `DomainSchemaError` if unknown."""
    for prop in _node_named(label, schema).properties:
        if prop.name == name:
            return prop
    raise DomainSchemaError(f"unknown property {name!r} on label {label!r}")


def patchable_fields(label: str, schema: Schema = DOMAIN_SCHEMA) -> frozenset[str]:
    """Return the names of `label`'s properties flagged neither identity nor lifecycle."""
    return frozenset(
        prop.name
        for prop in _node_named(label, schema).properties
        if not (prop.is_identity_bearing or prop.is_lifecycle_managed)
    )


def enum_values(label: str, name: str, schema: Schema = DOMAIN_SCHEMA) -> tuple[str, ...]:
    """Return an enum property's values in schema order; raise if it is not an enum."""
    prop = property_named(label, name, schema)
    if not isinstance(prop.type, EnumType):
        raise DomainSchemaError(f"property {name!r} on label {label!r} is not an enum")
    return prop.type.values
