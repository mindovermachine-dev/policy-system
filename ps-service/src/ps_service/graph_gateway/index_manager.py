"""`id` index management for the labels the gateway writes (issue #206, AC-BI-008).

Every `MATCH`/`MERGE` the gateway issues looks a node up by `id`, so each label needs an index on
it before its first load. Nothing is remembered between apply passes: each pass lists the graph's
indexes once and creates only the missing ones, so a flushed graph or a restore that swapped the
graph in heals on the next write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import redis.exceptions

from ps_service.graph_gateway.cypher import LIST_INDEXES, create_index_query
from ps_service.graph_gateway.errors import UnexpectedGraphReplyError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ps_service.ingestion.falkordb_client import GraphHandle

_REPLY_MESSAGE = "the graph answered an index listing with an unexpected shape"
_ALREADY_INDEXED = "already indexed"
_LABEL_COLUMN = 0
_PROPERTIES_COLUMN = 1
_ENTITY_COLUMN = 6
_NODE_ENTITY = "NODE"


def ensure_id_indexes(graph: GraphHandle, labels: Iterable[str]) -> None:
    """Create an index on `id` for each of `labels` that does not have one yet.

    Does nothing (not even a listing) when `labels` is empty. An index another writer created in
    the meantime counts as success.
    """
    wanted = sorted(set(labels))
    if not wanted:
        return
    indexed = _labels_indexed_on_id(graph)
    for label in wanted:
        if label not in indexed:
            _create_index(graph, label)


def _labels_indexed_on_id(graph: GraphHandle) -> set[str]:
    """Return the node labels that already have an index on `id`."""
    indexed: set[str] = set()
    for row in graph.query(LIST_INDEXES).result_set:
        if not isinstance(row, list) or len(cast("list[object]", row)) <= _PROPERTIES_COLUMN:
            raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
        columns = cast("list[object]", row)  # narrowed to a list; columns are checked on use
        label, properties = columns[_LABEL_COLUMN], columns[_PROPERTIES_COLUMN]
        if (
            isinstance(label, str)
            and isinstance(properties, list)
            and "id" in cast("list[object]", properties)
            and _is_node_index(columns)
        ):
            indexed.add(label)
    return indexed


def _is_node_index(columns: list[object]) -> bool:
    """Tell node indexes from relationship indexes when the reply says which it is."""
    return len(columns) <= _ENTITY_COLUMN or columns[_ENTITY_COLUMN] == _NODE_ENTITY


def _create_index(graph: GraphHandle, label: str) -> None:
    """Index `id` on `label`; "already indexed" means another writer got there first."""
    try:
        graph.query(create_index_query(label))
    except redis.exceptions.ResponseError as exc:
        if _ALREADY_INDEXED not in str(exc).lower():
            raise
