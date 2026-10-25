"""A read-only total of graph-log entries that are written but not yet applied.

Feeds the pending graph-log gauge. It is deliberately not a `PsycopgGraphLogStore` method: the
store records every read in the PS state Postgres health, and a metrics scrape must never change
what `/readyz` reports. This function opens its own short-lived, bounded, read-only connection and
touches no dependency-health state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.persistence import connect_from_config

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig

# Positions are gapless per graph (the `entries_next_position` trigger), so the last position
# minus the applied position is an exact count, from an index-only max per graph.
PENDING_ENTRIES_SQL = """
SELECT coalesce(sum(logged.last_position - coalesce(marker.applied_position, 0)), 0)
FROM (
    SELECT graph, max(position) AS last_position FROM graph_log.entries GROUP BY graph
) AS logged
LEFT JOIN graph_log.applied_markers AS marker ON marker.graph = logged.graph
WHERE logged.last_position > coalesce(marker.applied_position, 0)
"""


def count_pending_entries(
    config: ServiceConfig, *, connect_timeout_s: float, statement_timeout_ms: int
) -> int:
    """Count graph-log entries beyond each graph's applied position, summed over all graphs.

    Args:
        config: Supplies the PS state Postgres coordinates.
        connect_timeout_s: Longest the connection attempt may take.
        statement_timeout_ms: Server-side limit for the query.

    Returns:
        The total, `0` or more; no graph name is returned.

    Raises:
        StatePostgresConnectionError: The store is not configured.
        psycopg.Error: The connection or the query failed or timed out.

    Side Effects:
        Opens one read-only connection and closes it before returning. Records nothing in
        `ps_service.dependency_health`.
    """
    with connect_from_config(
        config, connect_timeout_s=connect_timeout_s, statement_timeout_ms=statement_timeout_ms
    ) as conn:
        conn.read_only = True
        row = conn.execute(PENDING_ENTRIES_SQL).fetchone()
    return max(int(row[0]), 0) if row is not None else 0
