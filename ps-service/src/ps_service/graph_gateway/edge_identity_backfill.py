"""Give the edges of a graph written before the gateway their identity (issue #208, F1).

The gateway merges an edge on its `identity`; an edge a pre-gateway writer left has none, so a
re-ingest would add a second, parallel edge beside it. `backfill_edge_identity` finds those
edges with a read-only scan and submits one `StampEdgeIdentity` group through the gateway, which
logs it under the caller's audit event id. The identity is `{type}:{source id}->{target id}`,
the formula the ingestion writers use, so the stamped edge is the one a later upsert merges into.

Idempotent: a second run finds no identity-less edge, submits nothing and logs nothing. Limit:
identity-less duplicates (same type and endpoints) all receive the same identity and stay
duplicates (the cardinality is preserved, not collapsed); the legacy writer merged on its
endpoints, so a graph it wrote should hold none. An edge whose type or end-point labels the
gateway may not name is left alone and counted in `skipped`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from ps_service.graph_gateway.cypher import UNIDENTIFIED_EDGE_SCAN
from ps_service.graph_gateway.errors import UnexpectedGraphReplyError
from ps_service.graph_gateway.gateway_log import emit_gateway_event
from ps_service.graph_gateway.label_allow_list import (
    ALLOWED_NODE_LABELS,
    ALLOWED_RELATIONSHIP_TYPES,
)
from ps_service.graph_gateway.models import MutationGroup, NodeRef, StampEdgeIdentity

if TYPE_CHECKING:
    from ps_service.graph_gateway.connection import WritableGraph
    from ps_service.graph_gateway.models import GroupOutcome
    from ps_service.logging import LogEmitter

SCAN_CHUNK_ROWS = 5000
"""Most edges one scan statement returns (each row is ids and names, no properties)."""

_ACTION = "edge_identity_backfill"
_ROW_WIDTH = 6
_REPLY_MESSAGE = "the graph answered the identity-less edge scan with an unexpected shape"


class _GroupSubmitting(Protocol):
    """The one call of the gateway the backfill needs."""

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        """Log and apply `group`."""
        ...


type UnidentifiedEdge = tuple[str, NodeRef, NodeRef]
"""`(type, source, target)` of an identity-less edge."""


class EdgeIdentityBackfillOutcome(BaseModel):
    """What one backfill run did."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["applied", "unchanged"]
    """`applied`: a group was submitted. `unchanged`: there was nothing to stamp; nothing logged."""
    stamped: int = Field(ge=0)
    """Identity-less (type, source, target) triples the submitted group stamps."""
    skipped: int = Field(ge=0)
    """Identity-less edges the gateway may not name (outside its allow-list); left untouched."""


def backfill_edge_identity(
    gateway: _GroupSubmitting,
    graph_name: str,
    audit_event_id: str | None,
    *,
    reader: WritableGraph,
    emitter: LogEmitter | None = None,
    chunk_rows: int = SCAN_CHUNK_ROWS,
) -> EdgeIdentityBackfillOutcome:
    """Stamp every identity-less edge of `graph_name` through `gateway`; `reader` only reads.

    Raises:
        UnexpectedGraphReplyError: the scan reply has a shape the gateway does not recognise.
        GraphWriteRejectedError: the gateway refused the group (its subclasses say why).
    """
    found, skipped = scan_unidentified_edges(reader, chunk_rows)
    if not found:
        outcome = EdgeIdentityBackfillOutcome(status="unchanged", stamped=0, skipped=skipped)
        _emit("unchanged", graph_name, 0, audit_event_id, emitter)
        return outcome
    gateway.submit_group(
        MutationGroup(
            graph=graph_name,
            audit_event_id=audit_event_id,
            primitives=tuple(
                legacy_edge_stamp(edge_type, source, target) for edge_type, source, target in found
            ),
        )
    )
    _emit("applied", graph_name, len(found), audit_event_id, emitter)
    return EdgeIdentityBackfillOutcome(status="applied", stamped=len(found), skipped=skipped)


def _emit(
    outcome: str,
    graph_name: str,
    count: int,
    audit_event_id: str | None,
    emitter: LogEmitter | None,
) -> None:
    fields: dict[str, str | int | float] = {"graph": graph_name, "entry_count": count}
    if audit_event_id is not None:
        fields["audit_event_id"] = audit_event_id
    emit_gateway_event(_ACTION, outcome, fields, emitter=emitter)


def legacy_edge_stamp(edge_type: str, source: NodeRef, target: NodeRef) -> StampEdgeIdentity:
    """Return the stamp giving the identity-less edge the identity `{type}:{source}->{target}`."""
    return StampEdgeIdentity(
        type=edge_type,
        identity=f"{edge_type}:{source.id}->{target.id}",
        source=source,
        target=target,
    )


def scan_unidentified_edges(
    reader: WritableGraph, chunk_rows: int = SCAN_CHUNK_ROWS
) -> tuple[list[UnidentifiedEdge], int]:
    """Return the distinct identity-less edges, in scan order, and how many were unnameable.

    Read-only. An edge whose type or end-point label the gateway may not name is counted, not
    returned.
    """
    found: dict[UnidentifiedEdge, None] = {}
    skipped = 0
    after = -1
    while True:
        result = reader.query(UNIDENTIFIED_EDGE_SCAN, {"after": after, "limit": chunk_rows})
        rows = [_row(item) for item in result.result_set]
        for cursor, edge_type, source_labels, source_id, target_labels, target_id in rows:
            stamp = _stamp(edge_type, source_labels, source_id, target_labels, target_id)
            if stamp is None:
                skipped += 1
            else:
                found.setdefault(stamp)
            after = max(after, cursor)
        if len(rows) < chunk_rows:
            return list(found), skipped


def _row(item: object) -> tuple[int, str, list[str], str, list[str], str]:
    """Return a scan row checked for shape, or raise `UnexpectedGraphReplyError`."""
    if not isinstance(item, list) or len(cast("list[object]", item)) != _ROW_WIDTH:
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    cursor, edge_type, source_labels, source_id, target_labels, target_id = cast(
        "list[object]", item
    )
    if (
        not isinstance(cursor, int)
        or not isinstance(edge_type, str)
        or not isinstance(source_id, str)
        or not isinstance(target_id, str)
    ):
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    return cursor, edge_type, _names(source_labels), source_id, _names(target_labels), target_id


def _names(value: object) -> list[str]:
    """Return a label list checked to hold only text, or raise `UnexpectedGraphReplyError`."""
    items = cast("list[object]", value) if isinstance(value, list) else None
    if items is None or not all(isinstance(item, str) for item in items):
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    return cast("list[str]", items)


def _stamp(
    edge_type: str,
    source_labels: list[str],
    source_id: str,
    target_labels: list[str],
    target_id: str,
) -> UnidentifiedEdge | None:
    """Name the edge to the gateway, or return None if its type or a label is not allowed."""
    source_label, target_label = _allowed_label(source_labels), _allowed_label(target_labels)
    if edge_type not in ALLOWED_RELATIONSHIP_TYPES or source_label is None or target_label is None:
        return None
    return (
        edge_type,
        NodeRef(label=source_label, id=source_id),
        NodeRef(label=target_label, id=target_id),
    )


def _allowed_label(labels: list[str]) -> str | None:
    """Return the first allow-listed label of a node (sorted, so the pick is stable), if any."""
    return next((label for label in sorted(labels) if label in ALLOWED_NODE_LABELS), None)
