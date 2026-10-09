"""Semantic structured logging of the Graph Write Gateway (issue #206, AC-BI-013).

Only identifiers and positions may be logged: never a payload, an embedding, a label, an
identity, exception text, a host or a credential. `emit_gateway_event` refuses any other key, so
a new field is a conscious edit of `ALLOWED_LOG_FIELDS`.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from ps_service.logging.errors import LoggingLifecycleError
from ps_service.logging.facade import emit_log_entry

if TYPE_CHECKING:
    from ps_service.logging import LogEmitter

COMPONENT = "graph_gateway"

ALLOWED_LOG_FIELDS = frozenset(
    {
        "graph",
        "first_position",
        "last_position",
        "entry_count",
        "attempt",
        "backoff_seconds",
        "error_class",
        "group_id",
        "audit_event_id",
    }
)
"""The only `extra` keys a gateway log entry may carry."""


def emit_gateway_event(
    action: str,
    outcome: str,
    fields: dict[str, str | int | float],
    *,
    emitter: LogEmitter | None,
) -> None:
    """Log one `action` entry of the gateway; `fields` must be a subset of `ALLOWED_LOG_FIELDS`."""
    unexpected = set(fields) - ALLOWED_LOG_FIELDS
    if unexpected:
        message = f"gateway log fields not allowed: {sorted(unexpected)}"
        raise ValueError(message)
    with contextlib.suppress(LoggingLifecycleError):
        emit_log_entry(
            component=COMPONENT,
            action=action,
            outcome=outcome,
            extra=dict(fields),
            emitter=emitter,
        )
