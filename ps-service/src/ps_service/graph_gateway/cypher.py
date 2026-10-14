"""Cypher the Graph Write Gateway sends to FalkorDB.

Every value travels as a parameter (`$rows`). The only interpolated text is a node label or a
relationship type, and it is checked here at the point of use (allow-list, then identifier shape)
even though validation already ran (a second validation layer for query construction).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ps_service.graph_gateway.exact_floats import PROPERTY_COLUMNS
from ps_service.graph_gateway.label_allow_list import (
    require_allowed_node_label,
    require_allowed_relationship_type,
)
from ps_service.graph_gateway.models import IDENTIFIER_PATTERN, RESERVED_NODE_PROPERTY_KEYS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ps_service.graph_gateway.models import (
        DeleteEdge,
        DeleteNode,
        MergeProperty,
        RemoveProperty,
        StampEdgeIdentity,
        UpsertEdge,
        UpsertNode,
    )

UPSERT_NODE_TEMPLATE = (
    "UNWIND $rows AS row MERGE (n:{L} {id: row.id}) SET n += row.properties "
    "SET n.embedding = coalesce(row.embedding, n.embedding)"
)
"""Upsert nodes of one label; a row without an embedding never wipes an existing one."""

MERGE_PROPERTY_TEMPLATE = "UNWIND $rows AS row MATCH (n:{L} {id: row.id}) SET n += row.properties"
"""Merge properties into existing nodes of one label."""

REMOVE_PROPERTY_TEMPLATE = "UNWIND $rows AS row MATCH (n:{L} {id: row.id}) REMOVE {KEYS}"
"""Remove properties from existing nodes of one label; `{KEYS}` is `n.k1, n.k2, ...`."""

DELETE_NODE_TEMPLATE = "UNWIND $rows AS row MATCH (n:{L} {id: row.id}) DETACH DELETE n"
"""Delete existing nodes of one label together with their relationships."""

UPSERT_EDGE_TEMPLATE = (
    "UNWIND $rows AS row MATCH (s:{SL} {id: row.source_id}), (t:{TL} {id: row.target_id}) "
    "MERGE (s)-[r:{T} {identity: row.identity}]->(t) SET r += row.properties"
)
"""Upsert edges of one type between existing nodes; `identity` is the merge key."""

DELETE_EDGE_TEMPLATE = (
    "UNWIND $rows AS row MATCH (s:{SL} {id: row.source_id})"
    "-[r:{T} {identity: row.identity}]->(t:{TL} {id: row.target_id}) DELETE r"
)
"""Delete the edges of one type that carry the given identities."""

STAMP_EDGE_IDENTITY_TEMPLATE = (
    "UNWIND $rows AS row MATCH (s:{SL} {id: row.source_id})"
    "-[r:{T}]->(t:{TL} {id: row.target_id}) WHERE r.identity IS NULL SET r.identity = row.identity"
)
"""Give the identity-less edges of one type between the given nodes their identity, in place.

Nothing is created: an edge that already has an identity is not matched, so a second run changes
nothing. Every identity-less edge between the same two nodes receives the row's identity.
"""

NODE_STATE_TEMPLATE = (
    "UNWIND $rows AS row MATCH (n:{L} {id: row.id}) WITH n, properties(n) AS p "
    f"RETURN n.id, {PROPERTY_COLUMNS}"
)
"""Read the properties (embedding included) of the given ids that exist as nodes of one label.

The last two columns carry every non-zero float exactly (see `exact_floats`): the reply prints a
double with 15 significant digits, so `p` alone cannot tell two stored values apart.
"""

NODE_EXISTS_TEMPLATE = "UNWIND $rows AS row MATCH (n:{L} {id: row.id}) RETURN n.id"
"""Read only the ids, of the given ids, that exist as nodes of one label (no properties).

For the endpoints of an edge: whether they exist is all a write needs to know, and a node's
properties can carry a 3,072-double embedding.
"""

EDGE_STATE_TEMPLATE = (
    "UNWIND $rows AS row MATCH (s:{SL} {id: row.source_id})"
    "-[r:{T} {identity: row.identity}]->(t:{TL} {id: row.target_id}) "
    "WITH row, properties(r) AS p "
    f"RETURN row.source_id, row.target_id, row.identity, {PROPERTY_COLUMNS}"
)
"""Read the properties of the given edges that exist, keyed by endpoints and identity.

The last two columns carry every non-zero float exactly, as in `NODE_STATE_TEMPLATE`.
"""

UNIDENTIFIED_EDGE_STATE_TEMPLATE = (
    "UNWIND $rows AS row MATCH (s:{SL} {id: row.source_id})"
    "-[r:{T}]->(t:{TL} {id: row.target_id}) WHERE r.identity IS NULL "
    "WITH row, properties(r) AS p "
    f"RETURN row.source_id, row.target_id, {PROPERTY_COLUMNS}"
)
"""Read the properties of the identity-less edges of one type between the given nodes."""

UNIDENTIFIED_EDGE_SCAN = (
    "MATCH (s)-[r]->(t) WHERE r.identity IS NULL AND id(r) > $after "
    "WITH s, r, t ORDER BY id(r) LIMIT $limit "
    "RETURN id(r), type(r), labels(s), s.id, labels(t), t.id"
)
"""One chunk of the edges that carry no identity (read-only); `$after` is the internal-id cursor."""

REPLAY_STATE_LABEL = "GraphReplayState"
"""Label of the replay-progress sentinel node: bookkeeping, never part of a digest."""

REPLAY_STATE_READ = f"MATCH (p:{REPLAY_STATE_LABEL}) RETURN p.position, p.state, p.kind, p.verified"
"""Read the replay-progress sentinel (at most one exists); no row means no replay is in progress."""

REPLAY_STATE_WRITE = (
    f"MERGE (p:{REPLAY_STATE_LABEL}) "
    "SET p.position = $position, p.state = $state, p.kind = $kind, p.verified = $verified"
)
"""Create or advance the replay-progress sentinel."""

REPLAY_STATE_DELETE = f"MATCH (p:{REPLAY_STATE_LABEL}) DELETE p"
"""Remove the replay-progress sentinel once the replay completed."""

GRAPH_HOLDS_A_NODE = f"MATCH (n) WHERE NOT n:{REPLAY_STATE_LABEL} RETURN id(n) LIMIT 1"
"""One row if the graph holds a node besides the replay sentinel; none if empty or missing."""

DIGEST_NODE_SCAN = (
    "MATCH (n) WHERE id(n) > $after AND NOT n:GraphReplayState "
    "WITH n ORDER BY id(n) LIMIT $limit WITH n, properties(n) AS p "
    f"RETURN id(n), labels(n), {PROPERTY_COLUMNS}"
)
"""One chunk of the node scan of a digest: `$after` is the internal-id cursor (never hashed)."""

DIGEST_EDGE_SCAN = (
    "MATCH (s)-[r]->(t) WHERE id(r) > $after "
    "WITH s, r, t ORDER BY id(r) LIMIT $limit WITH s, r, t, properties(r) AS p "
    f"RETURN id(r), type(r), labels(s), s.id, labels(t), t.id, {PROPERTY_COLUMNS}"
)
"""One chunk of the relationship scan of a digest; `$after` is the internal-id cursor."""

LIST_INDEXES = "CALL db.indexes()"
"""List the indexes of the graph."""

CREATE_INDEX_TEMPLATE = "CREATE INDEX FOR (n:{L}) ON (n.id)"
"""Index the `id` property of nodes of one label."""

_IDENTIFIER = re.compile(IDENTIFIER_PATTERN)


def require_safe_identifier(name: str) -> str:
    """Check that `name` is safe to interpolate into Cypher.

    Args:
        name: Candidate label, relationship type or property key.

    Returns:
        `name`, unchanged, when it matches the identifier pattern in full.

    Raises:
        ValueError: `name` is not a safe Cypher identifier.
    """
    if _IDENTIFIER.fullmatch(name) is None:
        message = "not a safe Cypher identifier"
        raise ValueError(message)
    return name


def _node_label(label: str) -> str:
    """Return `label` once it is allow-listed and identifier-safe."""
    return require_safe_identifier(require_allowed_node_label(label))


def _relationship_type(relationship_type: str) -> str:
    """Return `relationship_type` once it is allow-listed and identifier-safe."""
    return require_safe_identifier(require_allowed_relationship_type(relationship_type))


def upsert_node_query(label: str) -> str:
    """Build the upsert-node query for `label`.

    Args:
        label: Node label; must be allow-listed and identifier-safe.

    Returns:
        The query text with `label` interpolated; values travel as the `$rows` parameter.

    Raises:
        UnlistedNameError: `label` is not allow-listed.
        ValueError: `label` is not identifier-safe.
    """
    return UPSERT_NODE_TEMPLATE.replace("{L}", _node_label(label))


def create_index_query(label: str) -> str:
    """Build the query that indexes `id` on nodes of `label`.

    Args:
        label: Node label; must be allow-listed and identifier-safe.

    Returns:
        The query text with `label` interpolated; values travel as the `$rows` parameter.

    Raises:
        UnlistedNameError: `label` is not allow-listed.
        ValueError: `label` is not identifier-safe.
    """
    return CREATE_INDEX_TEMPLATE.replace("{L}", _node_label(label))


def merge_property_query(label: str) -> str:
    """Build the merge-property query for `label`.

    Args:
        label: Node label; must be allow-listed and identifier-safe.

    Returns:
        The query text with `label` interpolated; values travel as the `$rows` parameter.

    Raises:
        UnlistedNameError: `label` is not allow-listed.
        ValueError: `label` is not identifier-safe.
    """
    return MERGE_PROPERTY_TEMPLATE.replace("{L}", _node_label(label))


def delete_node_query(label: str) -> str:
    """Build the delete-node query for `label`.

    Args:
        label: Node label; must be allow-listed and identifier-safe.

    Returns:
        The query text with `label` interpolated; values travel as the `$rows` parameter.

    Raises:
        UnlistedNameError: `label` is not allow-listed.
        ValueError: `label` is not identifier-safe.
    """
    return DELETE_NODE_TEMPLATE.replace("{L}", _node_label(label))


def remove_property_query(label: str, keys: Sequence[str]) -> str:
    """Build the remove-property query for `label` and the property `keys`.

    Keys are the one non-label text interpolated into Cypher, so each is checked here at the
    point of use: identifier shape, and not a key the gateway owns.

    Args:
        label: Node label; must be allow-listed and identifier-safe.
        keys: Property keys to remove; each must be identifier-safe and not reserved.

    Returns:
        The query text with the label and the `n.key, ...` clause interpolated.

    Raises:
        UnlistedNameError: `label` is not allow-listed.
        ValueError: `label` or a key is not identifier-safe, or a key is reserved.
    """
    clause = ", ".join(f"n.{_removable_key(key)}" for key in keys)
    return REMOVE_PROPERTY_TEMPLATE.replace("{L}", _node_label(label)).replace("{KEYS}", clause)


def _removable_key(key: str) -> str:
    """Return `key` once it is identifier-safe and not reserved."""
    if key in RESERVED_NODE_PROPERTY_KEYS:
        message = "a reserved property key cannot be removed"
        raise ValueError(message)
    return require_safe_identifier(key)


def node_state_query(label: str) -> str:
    """Build the read that returns the state of nodes of `label`.

    Args:
        label: Node label; must be allow-listed and identifier-safe.

    Returns:
        The query text with `label` interpolated; values travel as the `$rows` parameter.

    Raises:
        UnlistedNameError: `label` is not allow-listed.
        ValueError: `label` is not identifier-safe.
    """
    return NODE_STATE_TEMPLATE.replace("{L}", _node_label(label))


def node_exists_query(label: str) -> str:
    """Build the read that returns which of the given ids exist as nodes of `label`.

    Args:
        label: Node label; must be allow-listed and identifier-safe.

    Returns:
        The query text with `label` interpolated; values travel as the `$rows` parameter.

    Raises:
        UnlistedNameError: `label` is not allow-listed.
        ValueError: `label` is not identifier-safe.
    """
    return NODE_EXISTS_TEMPLATE.replace("{L}", _node_label(label))


def edge_state_query(relationship_type: str, source_label: str, target_label: str) -> str:
    """Build the read that returns the state of edges of one type between two labels.

    Args:
        relationship_type: Relationship type; must be allow-listed and identifier-safe.
        source_label: Label of the source node; must be allow-listed and identifier-safe.
        target_label: Label of the target node; must be allow-listed and identifier-safe.

    Returns:
        The query text with the type and labels interpolated; values travel as `$rows`.

    Raises:
        UnlistedNameError: A label or the type is not allow-listed.
        ValueError: A label or the type is not identifier-safe.
    """
    return _edge_text(EDGE_STATE_TEMPLATE, relationship_type, source_label, target_label)


def unidentified_edge_state_query(
    relationship_type: str, source_label: str, target_label: str
) -> str:
    """Build the read that returns the identity-less edges of one type between two labels.

    Args:
        relationship_type: Relationship type; must be allow-listed and identifier-safe.
        source_label: Label of the source node; must be allow-listed and identifier-safe.
        target_label: Label of the target node; must be allow-listed and identifier-safe.

    Returns:
        The query text with the type and labels interpolated; values travel as `$rows`.

    Raises:
        UnlistedNameError: A label or the type is not allow-listed.
        ValueError: A label or the type is not identifier-safe.
    """
    return _edge_text(
        UNIDENTIFIED_EDGE_STATE_TEMPLATE, relationship_type, source_label, target_label
    )


def stamp_edge_identity_query(stamp: StampEdgeIdentity) -> str:
    """Build the stamp-identity query for the type and endpoint labels of `stamp`.

    Args:
        stamp: The identity stamp to apply; its type and endpoint labels must be allow-listed and
            identifier-safe.

    Returns:
        The query text with the type and endpoint labels interpolated; values travel as `$rows`.

    Raises:
        UnlistedNameError: A label or the type is not allow-listed.
        ValueError: A label or the type is not identifier-safe.
    """
    return _edge_query(STAMP_EDGE_IDENTITY_TEMPLATE, stamp)


def upsert_edge_query(edge: UpsertEdge | DeleteEdge) -> str:
    """Build the upsert-edge query for the type and endpoint labels of `edge`.

    Args:
        edge: The edge to upsert; its type and endpoint labels must be allow-listed and
            identifier-safe.

    Returns:
        The query text with the type and endpoint labels interpolated; values travel as `$rows`.

    Raises:
        UnlistedNameError: A label or the type is not allow-listed.
        ValueError: A label or the type is not identifier-safe.
    """
    return _edge_query(UPSERT_EDGE_TEMPLATE, edge)


def delete_edge_query(edge: UpsertEdge | DeleteEdge) -> str:
    """Build the delete-edge query for the type and endpoint labels of `edge`.

    Args:
        edge: The edge to delete; its type and endpoint labels must be allow-listed and
            identifier-safe.

    Returns:
        The query text with the type and endpoint labels interpolated; values travel as `$rows`.

    Raises:
        UnlistedNameError: A label or the type is not allow-listed.
        ValueError: A label or the type is not identifier-safe.
    """
    return _edge_query(DELETE_EDGE_TEMPLATE, edge)


def _edge_query(template: str, edge: UpsertEdge | DeleteEdge | StampEdgeIdentity) -> str:
    """Fill an edge template from the type and endpoint labels of `edge`."""
    return _edge_text(template, edge.type, edge.source.label, edge.target.label)


def _edge_text(template: str, relationship_type: str, source_label: str, target_label: str) -> str:
    """Fill the type and endpoint labels of an edge template, each checked at point of use."""
    return (
        template.replace("{SL}", _node_label(source_label))
        .replace("{TL}", _node_label(target_label))
        .replace("{T}", _relationship_type(relationship_type))
    )


def upsert_node_rows(nodes: Sequence[UpsertNode]) -> list[dict[str, object]]:
    """Build the `$rows` parameter for upserting `nodes`.

    Args:
        nodes: Node upserts of one label; empty yields no rows.

    Returns:
        One row per node with `id`, `properties` and `embedding`; an absent embedding is `None`.
    """
    return [
        {
            "id": node.id,
            "properties": dict(node.properties),
            "embedding": None if node.embedding is None else list(node.embedding),
        }
        for node in nodes
    ]


def upsert_edge_rows(edges: Sequence[UpsertEdge]) -> list[dict[str, object]]:
    """Build the `$rows` parameter for upserting `edges`.

    Args:
        edges: Edge upserts of one type and endpoint label pair; empty yields no rows.

    Returns:
        One row per edge with `source_id`, `target_id`, `identity` and `properties`.
    """
    return [
        {
            "source_id": edge.source.id,
            "target_id": edge.target.id,
            "identity": edge.identity,
            "properties": dict(edge.properties),
        }
        for edge in edges
    ]


def delete_edge_rows(edges: Sequence[DeleteEdge | StampEdgeIdentity]) -> list[dict[str, object]]:
    """Build the `$rows` parameter for deleting, or stamping the identity of, `edges`.

    Args:
        edges: Edge deletes or identity stamps of one type and endpoint label pair; empty yields
            no rows.

    Returns:
        One row per edge with `source_id`, `target_id` and `identity`.
    """
    return [
        {"source_id": edge.source.id, "target_id": edge.target.id, "identity": edge.identity}
        for edge in edges
    ]


def merge_property_rows(merges: Sequence[MergeProperty]) -> list[dict[str, object]]:
    """Build the `$rows` parameter for merging properties.

    Args:
        merges: Property merges of one label; empty yields no rows.

    Returns:
        One row per merge with the node `id` and the `properties` to merge.
    """
    return [{"id": merge.id, "properties": dict(merge.properties)} for merge in merges]


def node_id_rows(nodes: Sequence[RemoveProperty | DeleteNode]) -> list[dict[str, object]]:
    """Build the `$rows` parameter for removing properties from, or deleting, `nodes`.

    Args:
        nodes: Property removals or node deletes of one label; empty yields no rows.

    Returns:
        One row per node with its `id`.
    """
    return [{"id": node.id} for node in nodes]


def node_state_rows(ids: Sequence[str]) -> list[dict[str, object]]:
    """Build the `$rows` parameter of a node state read.

    Args:
        ids: Node ids to read; empty yields no rows.

    Returns:
        One row with the node `id` per id.
    """
    return [{"id": node_id} for node_id in ids]


def unidentified_edge_state_rows(edges: Sequence[tuple[str, str]]) -> list[dict[str, object]]:
    """Build the `$rows` parameter of an identity-less edge read.

    Args:
        edges: `(source id, target id)` pairs; empty yields no rows.

    Returns:
        One row with `source_id` and `target_id` per pair.
    """
    return [{"source_id": source_id, "target_id": target_id} for source_id, target_id in edges]


def edge_state_rows(edges: Sequence[tuple[str, str, str]]) -> list[dict[str, object]]:
    """Build the `$rows` parameter of an edge state read.

    Args:
        edges: `(source id, target id, identity)` triples; empty yields no rows.

    Returns:
        One row with `source_id`, `target_id` and `identity` per triple.
    """
    return [
        {"source_id": source_id, "target_id": target_id, "identity": identity}
        for source_id, target_id, identity in edges
    ]
