"""Record shape for `ps_service.logging`: what a log entry is. No I/O, no context.

`LogEntry` is the single definition of the field convention (component, action, entity id(s),
outcome, duration in milliseconds) plus run id, timestamp and `extra`. It is a convention,
not an enforced schema: `extra` carries anything else, and the caller owns keeping secrets
and personal data out of every field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from time import time

# A single entity id, a tuple of ids, or absent; emitted as-is.
type EntityId = str | tuple[str, ...] | None

_RESERVED_KEYS = frozenset(
    {"component", "action", "run_id", "entity_id", "outcome", "duration_ms", "timestamp"}
)


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One structured, JSON-serializable log event (a convention, not a schema).

    Attributes:
        component: Name of the emitting component; required.
        action: Name of the operation being logged; required.
        run_id: Correlation id of the run the entry belongs to, or None when no run is bound.
        entity_id: One entity id, a tuple of entity ids, or None. Emitted as given.
        outcome: Result label such as `"success"` or `"failed"`, or None.
        duration_ms: Elapsed time in milliseconds, or None.
        timestamp: Seconds since the Unix epoch; defaults to the time of construction.
        extra: Additional key/value pairs, kept as an immutable tuple of pairs. Keys that
            collide with a convention field name are dropped when rendering. Values must be
            JSON-serializable to be written normally; never place secrets or personal data
            here.
    """

    component: str
    action: str
    run_id: str | None = None
    entity_id: EntityId = None
    outcome: str | None = None
    duration_ms: float | None = None
    timestamp: float = field(default_factory=time)
    # Immutable pairs, not a dict: a frozen dataclass holding a dict is only
    # reassignment-frozen, so a caller could still mutate `entry.extra["x"]`.
    # `emit_log_entry` converts its mapping argument once at the boundary.
    extra: tuple[tuple[str, object], ...] = field(default_factory=tuple)

    def to_json_line(self) -> str:
        r"""Render this entry as one JSON object on one line, terminated by a newline.

        Business Rules:
            An optional convention field (`run_id`, `entity_id`, `outcome`, `duration_ms`)
            whose value is None is omitted rather than emitted as `null`. `component`,
            `action` and `timestamp` are always present. An `extra` key that collides with a
            convention field name is dropped, so the convention value always wins. Keys are
            sorted and non-ASCII text is kept unescaped.

        Returns:
            The JSON text followed by a single `"\n"`.

        Raises:
            TypeError: A value in `extra` (or an `entity_id` element) is not JSON-serializable.
            ValueError: The entry contains a circular reference.
        """
        payload: dict[str, object] = {"component": self.component, "action": self.action}
        if self.run_id is not None:
            payload["run_id"] = self.run_id
        if self.entity_id is not None:
            payload["entity_id"] = self.entity_id  # str or tuple[str, ...], emitted as given
        if self.outcome is not None:
            payload["outcome"] = self.outcome
        if self.duration_ms is not None:
            payload["duration_ms"] = self.duration_ms
        payload["timestamp"] = self.timestamp
        payload.update({key: value for key, value in self.extra if key not in _RESERVED_KEYS})
        return json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
