"""The internal-regulation intake JSON Schema, derived from the base schema.

`INTAKE_PROFILE` expresses how the intake format differs from the full graph schema
(system-minted properties and edges are absent, `source_type` is fixed to `internal`,
`confidence` is optional everywhere). `intake_property_defs` turns the profiled nodes
into the per-label `*Properties` JSON Schema definitions.
`generate_intake_schema` assembles the whole file; the fixed skeleton (id, title, prose
description, `confidence`, `nodeRef`, `node`) lives here as constants.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.domain_schema.errors import DomainSchemaError
from ps_service.domain_schema.json_layout import Json, dumps_hand_layout
from ps_service.domain_schema.model import (
    ConstType,
    DateType,
    EnumType,
    FloatRangeType,
    Presence,
    Property,
    StringType,
)
from ps_service.domain_schema.profile import Add, Narrow, Omit, Profile, apply_profile

if TYPE_CHECKING:
    from ps_service.domain_schema.model import Node, PropertyType, Schema

type JsonObject = dict[str, Json]

CONFIDENCE_REF = "#/$defs/confidence"
_SCHEMA_ID = "https://policy-system.internal/schemas/internal-regulation-intake.v1.schema.json"
_DESCRIPTION = (
    "Structural allow-list schema for "
    "docs/artifacts/internal-regulation-intake-format.md (issue #54, D7). Validates node "
    "label / edge type enums, per-label required properties and their JSON types, and "
    "additionalProperties:false at every level -- including the top level, so a "
    "submitted 'graph_name' or any other unrecognized field is rejected outright (D2). "
    "Node shape is dispatched per-label via if/then (not oneOf) deliberately: oneOf's 5 "
    "parallel branches would each fail similarly-shallowly on a wrong-shaped node, "
    "giving validators no unambiguous 'best' error to surface; if/then instead evaluates "
    "exactly the one branch matching the node's own 'label', so a violation's reported "
    "path/message stays specific (AC-BI-019). Does NOT validate cross-node referential "
    "integrity (dangling edges) or cardinality rules (e.g. exactly one HAS per "
    "Obligation) -- those are the internal-seed adapter's own semantic-validation job at "
    "persist time, not a JSON document shape concern."
)
_CONFIDENCE_DESCRIPTION = (
    "Optional on every node; defaults to 1.0 when omitted "
    "(not enforced by this schema, an adapter-side default)."
)
_UNIT_INTERVAL = FloatRangeType(minimum=0.0, maximum=1.0)
_OPTIONAL_CONFIDENCE = Property("confidence", _UNIT_INTERVAL, Presence.OPTIONAL)
_RELAXED_CONFIDENCE_LABELS = ("Role", "Requirement", "Obligation", "Capability")

_NODE_ORDER = (
    "RegulatoryInstrument",
    "Role",
    "Requirement",
    "Obligation",
    "Capability",
    "Policy",
    "Standard",
    "Control",
    "PracticeArea",
    "RiskPath",
)
_EDGE_ORDER = (
    ("DEFINES", "RegulatoryInstrument", "Role"),
    ("EXPRESSES", "RegulatoryInstrument", "Requirement"),
    ("HAS", "Role", "Obligation"),
    ("SATISFIED_BY", "Requirement", "Obligation"),
    ("REQUIRES", "Obligation", "Capability"),
    ("GOVERNED_BY", "Capability", "Policy"),
    ("SUPPORTED_BY", "Policy", "Standard"),
    ("IMPLEMENTED_BY", "Standard", "Control"),
    ("COVERS", "PracticeArea", "Capability"),
    ("VERIFIED_BY", "RiskPath", "Control"),
    ("OWNS", "PracticeArea", "Policy"),
    ("MITIGATED_BY", "RiskPath", "Capability"),
)
# Minted by the system (change monitor, domain mapper, cleanup), never submitted.
_SYSTEM_MINTED_EDGES = (
    ("SUPERSEDED_BY", "RegulatoryInstrument", "RegulatoryInstrument"),
    ("SUPERSEDED_BY", "Policy", "Policy"),
    ("TRANSPOSES", "RegulatoryInstrument", "RegulatoryInstrument"),
    ("MERGED_INTO", "Capability", "Capability"),
)

# Layout quirk of the committed file: every label lists required properties first except
# these two, whose required `status` stays in schema order (after `description`).
_SCHEMA_ORDER_LABELS = ("PracticeArea", "RiskPath")

INTAKE_PROFILE = Profile(
    name="internal-regulation-intake-v1",
    operations=(
        Narrow("RegulatoryInstrument", "source_type", ConstType("internal")),
        Omit("RegulatoryInstrument", "id"),
        Omit("RegulatoryInstrument", "instrument_type"),
        Add("RegulatoryInstrument", _OPTIONAL_CONFIDENCE),
        Omit("Standard", "status"),
        Omit("Control", "status"),
        Omit("Capability", "status"),
        Narrow("Policy", "status", EnumType(("draft", "approved", "deprecated"))),
        Narrow("Capability", "type", StringType(min_length=1)),
        *(
            operation
            for label in _RELAXED_CONFIDENCE_LABELS
            for operation in (Omit(label, "confidence"), Add(label, _OPTIONAL_CONFIDENCE))
        ),
    ),
    node_order=_NODE_ORDER,
    edge_order=_EDGE_ORDER,
    omitted_edges=_SYSTEM_MINTED_EDGES,
)


def property_json(field_type: PropertyType, *, required: bool) -> JsonObject:
    """Map a property type to its JSON Schema fragment."""
    match field_type:
        case StringType(min_length=min_length):
            minimum = max(min_length, 1 if required else 0)
            return {"type": "string", "minLength": minimum} if minimum else {"type": "string"}
        case DateType():
            return {"type": "string", "format": "date"}
        case FloatRangeType():
            if field_type != _UNIT_INTERVAL:
                raise DomainSchemaError(f"intake supports only the unit interval, got {field_type}")
            return {"$ref": CONFIDENCE_REF}
        case EnumType(values=values):
            return {"enum": list(values)}
        case ConstType(value=value):
            return {"const": value}


def _def_name(label: str) -> str:
    return f"{label[0].lower()}{label[1:]}Properties"


def _in_committed_order(node: Node) -> list[Property]:
    if node.label in _SCHEMA_ORDER_LABELS:
        return list(node.properties)
    return sorted(node.properties, key=lambda p: p.presence is not Presence.REQUIRED)


def _node_def(node: Node) -> JsonObject:
    ordered = _in_committed_order(node)
    required = [p.name for p in ordered if p.presence is Presence.REQUIRED]
    definition: JsonObject = {"type": "object", "additionalProperties": False}
    if required:
        definition["required"] = required
    definition["properties"] = {
        p.name: property_json(p.type, required=p.presence is Presence.REQUIRED) for p in ordered
    }
    return definition


def intake_property_defs(schema: Schema) -> dict[str, JsonObject]:
    """Return `<label>Properties` -> JSON Schema definition for the intake-profiled `schema`."""
    profiled = apply_profile(schema, INTAKE_PROFILE)
    return {_def_name(node.label): _node_def(node) for node in profiled.nodes}


def _ref(name: str) -> JsonObject:
    return {"$ref": f"#/$defs/{name}"}


def _node_dispatch(labels: list[str]) -> list[Json]:
    """One `if label == X then properties is <X>Properties` entry per label, in order."""
    return [
        {
            "if": {"properties": {"label": {"const": label}}},
            "then": {"properties": {"properties": _ref(_def_name(label))}},
        }
        for label in labels
    ]


def _edge_def(profiled: Schema) -> JsonObject:
    types = list(dict.fromkeys(edge.type for edge in profiled.edges))
    properties: dict[str, Json] = {}
    for edge in profiled.edges:
        for prop in edge.properties:
            properties.setdefault(prop.name, property_json(prop.type, required=False))
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "from", "to"],
        "properties": {
            "type": {"enum": types},
            "from": _ref("nodeRef"),
            "to": _ref("nodeRef"),
            "properties": {
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
            },
        },
    }


def _fixed_defs(labels: list[str]) -> dict[str, Json]:
    string_id: JsonObject = {"type": "string", "minLength": 1}
    return {
        "confidence": {
            "type": "number",
            "minimum": _UNIT_INTERVAL.minimum,
            "maximum": _UNIT_INTERVAL.maximum,
            "description": _CONFIDENCE_DESCRIPTION,
        },
        "nodeRef": {
            "type": "object",
            "additionalProperties": False,
            "required": ["label", "id"],
            "properties": {"label": _ref("nodeLabel"), "id": string_id},
        },
        "nodeLabel": {"enum": labels},
        "node": {
            "type": "object",
            "additionalProperties": False,
            "required": ["label", "id", "properties"],
            "properties": {
                "label": _ref("nodeLabel"),
                "id": string_id,
                "properties": {"type": "object"},
            },
            "allOf": _node_dispatch(labels),
        },
    }


def generate_intake_schema(schema: Schema) -> str:
    """Return the intake JSON Schema text for `schema`, in the committed hand layout."""
    profiled = apply_profile(schema, INTAKE_PROFILE)
    labels = [node.label for node in profiled.nodes]
    document: dict[str, Json] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": _SCHEMA_ID,
        "title": "Internal Regulation Intake Format v1",
        "description": _DESCRIPTION,
        "type": "object",
        "additionalProperties": False,
        "required": ["nodes", "edges"],
        "properties": {
            "nodes": {"type": "array", "items": _ref("node")},
            "edges": {"type": "array", "items": _ref("edge")},
        },
        "$defs": {
            **_fixed_defs(labels),
            **intake_property_defs(schema),
            "edge": _edge_def(profiled),
        },
    }
    return dumps_hand_layout(document)
