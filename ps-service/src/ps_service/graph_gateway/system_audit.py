"""The system audit row for writers that have no principal (issue #208).

Every `MutationGroup` carries the id of the `audit_events` row of the command that caused it.
Most commands already record such a row for their caller; an ungated one (the internal-seed
ingestion route) has none and no user to attribute one to. `record_system_audit` inserts one row
with a fixed system actor on its own connection and returns its id.

It is fail-closed: if the row cannot be written there is no id to attach, so nothing may be
written to the graph, and the caller sees `AuditTrailUnavailableError` (503). The row's `details`
are the caller's: this module adds no content of its own. The shared `audit` component is left
untouched (its Protocols, `record_standalone` and `record_opening_row`), as this module only
inserts through its own SQL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import psycopg
from psycopg.types.json import Json

from ps_service.audit.errors import AuditTrailUnavailableError
from ps_service.graph_gateway.gateway_log import emit_gateway_event
from ps_service.persistence import StatePostgresConnectionError, connect_from_config

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from psycopg.rows import TupleRow

    from ps_service.config import ServiceConfig
    from ps_service.logging import LogEmitter

SYSTEM_ACTOR_SUBJECT = "system:ingestion"
SYSTEM_ACTOR_ISSUER = "system"

_UNAVAILABLE_MESSAGE = "The audit trail is temporarily unavailable."
_LOG_ACTION = "system_audit"

_INSERT_SYSTEM_AUDIT_EVENT = """
INSERT INTO audit_events (
    actor_subject, actor_issuer, action, resource_type, resource_id, outcome, details
) VALUES (
    %(actor_subject)s, %(actor_issuer)s, %(action)s, %(resource_type)s, %(resource_id)s,
    'applied', %(details)s
)
RETURNING id
"""


class SystemAuditRecorder(Protocol):
    """Records one system audit row and returns its id (what a caller injects)."""

    def __call__(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        details: Mapping[str, object],
    ) -> str:
        """Insert the row; return its `audit_events.id`."""
        ...


def record_system_audit(
    connection_factory: Callable[[], psycopg.Connection[TupleRow]],
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    details: Mapping[str, object],
    emitter: LogEmitter | None = None,
) -> str:
    """Insert one `applied` audit row for the system actor and return its id (a UUID string).

    Raises:
        AuditTrailUnavailableError: the connection could not be opened or the insert failed.
            The message is fixed (no host, SQL or driver text); the cause is chained.
    """
    try:
        with connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                _INSERT_SYSTEM_AUDIT_EVENT,
                {
                    "actor_subject": SYSTEM_ACTOR_SUBJECT,
                    "actor_issuer": SYSTEM_ACTOR_ISSUER,
                    "action": action,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "details": Json(dict(details)),
                },
            )
            inserted = cursor.fetchone()
            if inserted is None:  # pragma: no cover - INSERT ... RETURNING always yields its row
                message = "the system audit insert returned no row"
                raise psycopg.DatabaseError(message)
            connection.commit()
    except (StatePostgresConnectionError, psycopg.Error) as exc:
        emit_gateway_event(
            _LOG_ACTION, "failed", {"error_class": type(exc).__name__}, emitter=emitter
        )
        raise AuditTrailUnavailableError(_UNAVAILABLE_MESSAGE) from exc
    audit_event_id = str(inserted[0])
    emit_gateway_event(
        _LOG_ACTION, "succeeded", {"audit_event_id": audit_event_id}, emitter=emitter
    )
    return audit_event_id


def build_system_audit_recorder(config: ServiceConfig) -> SystemAuditRecorder:
    """Bind `record_system_audit` to the PS state Postgres of `config` (opened on each call)."""

    def record(
        *, action: str, resource_type: str, resource_id: str, details: Mapping[str, object]
    ) -> str:
        return record_system_audit(
            lambda: connect_from_config(config),
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
        )

    return record
