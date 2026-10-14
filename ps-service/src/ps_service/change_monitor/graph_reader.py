"""Enumerate the tracked regulatory-instrument set from `policy_system`.

`poll_for_amendments` polls one instrument at a time; this module is the one
place that reads *which* instruments to poll. It runs a single fixed,
non-parameterized Cypher read against the merged single-tenant graph
(`ps_service.change_monitor.falkordb_client.single_tenant_graph_name`) and
maps each row to a `TrackedInstrumentNode`.

The query filters to `status = 'active'`, `source_type = 'external'`, and
`instrument_type IN ['regulation', 'directive']` -- so a
`national_transposition` node, an internal node, or a
superseded node is never enumerated and therefore never polled. Every
literal in the query is a fixed part of this module's query constant, never
an interpolated value (L2 "parameterize Cypher"; the label / property names
and the two `instrument_type` values are schema constants, not caller
input).

Reads only -- this module issues no write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ps_service.change_monitor.models import TrackedInstrumentNode

if TYPE_CHECKING:
    from ps_service.change_monitor.falkordb_client import GraphHandle

_TRACKED_INSTRUMENTS_QUERY = """\
MATCH (n:RegulatoryInstrument)
WHERE n.status = 'active'
  AND n.source_type = 'external'
  AND n.instrument_type IN ['regulation', 'directive']
RETURN n.id AS id, n.celex AS celex, n.instrument_type AS instrument_type,
       n.effective_date AS effective_date"""


def read_tracked_instruments(graph: GraphHandle) -> tuple[TrackedInstrumentNode, ...]:
    """Enumerate every active, external `regulation` / `directive` instrument.

    Provides the set of instruments `poll_for_amendments` checks for newer
    versions; superseded, internal and `national_transposition` nodes are
    excluded so they are never polled.

    Args:
        graph: Handle onto the merged `policy_system` graph. Must expose
            `query(cypher)` returning a result with `result_set`.

    Returns:
        One `TrackedInstrumentNode` per matching node, in the graph's result
        order; an empty tuple when no instrument is tracked. `celex` is `None`
        for a node without the optional `celex` property; `effective_date` is
        the empty string when the node has none.

    Side Effects:
        Runs one read-only Cypher query; issues no write.
    """
    result = graph.query(_TRACKED_INSTRUMENTS_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    return tuple(_row_to_node(row) for row in rows)


def _row_to_node(row: list[object]) -> TrackedInstrumentNode:
    """Map one `[id, celex, instrument_type, effective_date]` result row."""
    identifier, celex, instrument_type, effective_date = row
    return TrackedInstrumentNode(
        regulatory_instrument_id=_text(identifier),
        celex=_optional_text(celex),
        instrument_type=_text(instrument_type),
        effective_date=_text(effective_date),
    )


def _text(value: object) -> str:
    """The cell as a string, or the empty string when it is not one (e.g. null)."""
    return value if isinstance(value, str) else ""


def _optional_text(value: object) -> str | None:
    """The cell as a string, or `None` when it is not one (e.g. null)."""
    return value if isinstance(value, str) else None
