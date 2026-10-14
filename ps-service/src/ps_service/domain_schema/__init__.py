"""Code-defined PS domain schema and its renderers.

`DOMAIN_SCHEMA` is the single definition of the compliance graph's node labels, properties, edge
types and cardinalities. Documents, the intake JSON Schema and the slim schema served to LLM
clients are rendered from it, and hand-written vocabularies elsewhere are pinned to it by tests.

Importing this package is side-effect free and performs no I/O; the model, derivation helpers,
profile application and slim renderer re-exported here are pure functions of their arguments.
"""

from __future__ import annotations

from ps_service.domain_schema.definition import DOMAIN_SCHEMA
from ps_service.domain_schema.derivation import enum_values, patchable_fields, property_named
from ps_service.domain_schema.errors import DomainSchemaError, SchemaProfileError
from ps_service.domain_schema.model import (
    Cardinality,
    ConstType,
    DateType,
    Edge,
    EdgeProperty,
    EnumType,
    FloatRangeType,
    Multiplicity,
    Node,
    Presence,
    Property,
    PropertyType,
    Schema,
    StringType,
)
from ps_service.domain_schema.profile import Add, Narrow, Omit, Profile, apply_profile
from ps_service.domain_schema.render_slim import render_slim_schema

__all__ = [
    "DOMAIN_SCHEMA",
    "Add",
    "Cardinality",
    "ConstType",
    "DateType",
    "DomainSchemaError",
    "Edge",
    "EdgeProperty",
    "EnumType",
    "FloatRangeType",
    "Multiplicity",
    "Narrow",
    "Node",
    "Omit",
    "Presence",
    "Profile",
    "Property",
    "PropertyType",
    "Schema",
    "SchemaProfileError",
    "StringType",
    "apply_profile",
    "enum_values",
    "patchable_fields",
    "property_named",
    "render_slim_schema",
]
