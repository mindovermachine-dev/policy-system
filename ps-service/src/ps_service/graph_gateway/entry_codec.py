"""Pure encoding of graph primitives into log entries and back (issue #206).

The log store has no operation column, so each entry's `content` is an envelope whose reserved
`op` key names the primitive. The entry's `name` is the label (or relationship type), its
`identity` the caller-supplied id (or edge identity), and its `embedding` the node embedding.
Apply and replay share `decode_entry`, so a replayed log and a live apply cannot disagree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from ps_service.graph_gateway.errors import GraphLogEntryDecodeError
from ps_service.graph_gateway.models import (
    DeleteEdge,
    DeleteNode,
    GraphLogEntryDraft,
    MergeProperty,
    RemoveProperty,
    UpsertEdge,
    UpsertNode,
)

if TYPE_CHECKING:
    from ps_service.graph_gateway.models import GraphLogEntry, Primitive


def encode_primitive(primitive: Primitive) -> GraphLogEntryDraft:
    """Encode `primitive` as the log entry draft that records it."""
    match primitive:
        case UpsertNode():
            return GraphLogEntryDraft(
                name=primitive.label,
                identity=primitive.id,
                content={"op": primitive.op, "properties": dict(primitive.properties)},
                embedding=primitive.embedding,
            )
        case UpsertEdge():
            return GraphLogEntryDraft(
                name=primitive.type,
                identity=primitive.identity,
                content={
                    "op": primitive.op,
                    "source": primitive.source.model_dump(),
                    "target": primitive.target.model_dump(),
                    "properties": dict(primitive.properties),
                },
            )
        case MergeProperty():
            return GraphLogEntryDraft(
                name=primitive.label,
                identity=primitive.id,
                content={"op": primitive.op, "properties": dict(primitive.properties)},
            )
        case RemoveProperty():
            return GraphLogEntryDraft(
                name=primitive.label,
                identity=primitive.id,
                content={"op": primitive.op, "keys": list(primitive.keys)},
            )
        case DeleteNode():
            return GraphLogEntryDraft(
                name=primitive.label, identity=primitive.id, content={"op": primitive.op}
            )
        case DeleteEdge():
            return GraphLogEntryDraft(
                name=primitive.type,
                identity=primitive.identity,
                content={
                    "op": primitive.op,
                    "source": primitive.source.model_dump(),
                    "target": primitive.target.model_dump(),
                },
            )


def decode_entry(entry: GraphLogEntry) -> Primitive:
    """Decode a recorded `entry` back into the primitive it records.

    Raises:
        GraphLogEntryDecodeError: the entry's operation is unknown or its content is malformed.
    """
    content = entry.content
    try:
        match content.get("op"):
            case "upsert_node":
                return UpsertNode.model_validate(
                    {
                        "label": entry.name,
                        "id": entry.identity,
                        "properties": content.get("properties"),
                        "embedding": entry.embedding,
                    }
                )
            case "upsert_edge":
                return UpsertEdge.model_validate(_edge_fields(entry, with_properties=True))
            case "delete_edge":
                return DeleteEdge.model_validate(_edge_fields(entry, with_properties=False))
            case "merge_property" | "remove_property" | "delete_node":
                return _node_primitive(entry)
            case _:
                raise GraphLogEntryDecodeError(_failure_message(entry))
    except ValidationError as exc:
        raise GraphLogEntryDecodeError(_failure_message(entry)) from exc


_NODE_OPERATIONS: dict[str, type[MergeProperty | RemoveProperty | DeleteNode]] = {
    "merge_property": MergeProperty,
    "remove_property": RemoveProperty,
    "delete_node": DeleteNode,
}


def _node_primitive(entry: GraphLogEntry) -> MergeProperty | RemoveProperty | DeleteNode:
    """Decode an entry that acts on one existing node: merge, remove property or delete."""
    content = entry.content
    fields: dict[str, object] = {"label": entry.name, "id": entry.identity}
    if "properties" in content:
        fields["properties"] = content["properties"]
    if "keys" in content:
        fields["keys"] = content["keys"]
    return _NODE_OPERATIONS[str(content["op"])].model_validate(fields)


def _edge_fields(entry: GraphLogEntry, *, with_properties: bool) -> dict[str, object]:
    """Assemble the model fields of an edge entry from its columns and envelope."""
    fields: dict[str, object] = {
        "type": entry.name,
        "identity": entry.identity,
        "source": entry.content.get("source"),
        "target": entry.content.get("target"),
    }
    if with_properties:
        fields["properties"] = entry.content.get("properties")
    return fields


def _failure_message(entry: GraphLogEntry) -> str:
    """Name the entry by graph and position only, never by content."""
    return f"log entry {entry.position} of graph {entry.graph} does not decode"
