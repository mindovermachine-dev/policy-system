"""Typed `details` model of the `query.cypher` audit action.

Registers the model with `ps_service.audit.models.register_audit_action` at import time and
registers the `query` audit resource type. The `run-cypher` MCP tool is the emitter, so the action
lives here and not in `ps_service.query_engine`: the engine and the other in-process callers of
`execute_cypher_query` (for example the ingestion-status check) write no row, and the engine does
not depend on the audit package. `ps_service.mcp_interface` imports this module, so the action is
registered before the tool writes its row.

One row is written per `run-cypher` tool call, with the outcome in the `outcome` column
(`applied`, `rejected` or `failed`). The details never carry the query text or a database error
text: only the row count, the truncation flag and an enumerated reason code.

Side Effects:
    Importing this module registers `query.cypher` as an audit action and `query` as an audit
    resource type. Registration is process-global.

Raises:
    ValueError: at import time if the action name is already registered.
"""

from __future__ import annotations

from typing import Literal

from ps_service.audit.models import (
    AuditDetails,
    register_audit_action,
    register_audit_resource_type,
)

QUERY_CYPHER_ACTION = "query.cypher"
QUERY_RESOURCE_TYPE = "query"
QUERY_CYPHER_RESOURCE_ID = "cypher"

type QueryCypherReasonCode = Literal[
    "write_clause", "graph_unseeded", "query_failed", "graph_unavailable"
]


class QueryCypherDetails(AuditDetails):
    """Details of a `query.cypher` audit event.

    With `outcome='applied'` the row count and truncation flag are set and `reason_code` is
    `None`. With `outcome='rejected'` or `'failed'` only `reason_code` is set.

    Attributes:
        row_count: Number of rows returned to the caller, or `None` when the query did not run
            to completion.
        truncated: Whether the configured row cap cut the result, or `None` when the query did
            not run to completion.
        reason_code: `"write_clause"` (rejected: the query contains a write clause),
            `"graph_unseeded"` (the graph has no content), `"query_failed"` (the database
            rejected or failed the query, a server-side timeout included) or
            `"graph_unavailable"` (the graph database cannot be reached); `None` when applied.
    """

    row_count: int | None = None
    truncated: bool | None = None
    reason_code: QueryCypherReasonCode | None = None


register_audit_action(QUERY_CYPHER_ACTION, QueryCypherDetails)
register_audit_resource_type(QUERY_RESOURCE_TYPE)
