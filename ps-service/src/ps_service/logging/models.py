"""ps_service.logging record shape — what a log entry *is*. No I/O, no context.

`LogEntry` is the single source of truth for AC#3's field convention
(component, action, entity_id(s), outcome, duration_ms) plus plumbing
(run_id, timestamp, extra). It is a convention, not an enforced schema (arch
decision) — `extra` is the caller's escape hatch for anything else; the
caller owns PII hygiene there (never place secrets/PII in any field).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from time import time

# D1: entity_id(s) — a single id, a tuple of ids, or absent. Emitted as-is.
type EntityId = str | tuple[str, ...] | None

_RESERVED_KEYS = frozenset(
    {"component", "action", "run_id", "entity_id", "outcome", "duration_ms", "timestamp"}
)


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One structured, JSON-serializable log event (a convention, not a schema)."""

    component: str
    action: str
    run_id: str | None = None
    entity_id: EntityId = None
    outcome: str | None = None
    duration_ms: float | None = None
    timestamp: float = field(default_factory=time)
    # Immutable pairs, not a dict (M14 fix): a frozen dataclass with a
    # mutable-dict default is only reassignment-frozen, not
    # mutation-frozen — a caller could still `entry.extra["x"] = 1`. The
    # facade's public `emit_log_entry(extra: Mapping[str, object] | None)`
    # converts once at the boundary via `tuple(extra.items())`.
    extra: tuple[tuple[str, object], ...] = field(default_factory=tuple)

    def to_json_line(self) -> str:
        """Render this entry as one JSON object, one line, terminated by '\\n'.

        D2: a convention field whose value is `None` is omitted, not emitted
        as `null`. B1 fix: `entity_id` is included exactly like every other
        populated field below — there is no unconditional `.pop()` after it
        is added (PLAN.md's bug: the comprehension already dropped nulls,
        then a stray `pop("entity_id", None)` deleted it unconditionally
        even when set). M10 fix: an `extra` key colliding with a convention
        field name is dropped, not merged over it — the convention value
        always wins, deterministically.
        """
        payload: dict[str, object] = {"component": self.component, "action": self.action}
        if self.run_id is not None:
            payload["run_id"] = self.run_id
        if self.entity_id is not None:
            payload["entity_id"] = self.entity_id  # str or tuple[str, ...] — emitted as-is (D1)
        if self.outcome is not None:
            payload["outcome"] = self.outcome
        if self.duration_ms is not None:
            payload["duration_ms"] = self.duration_ms
        payload["timestamp"] = self.timestamp
        payload.update({key: value for key, value in self.extra if key not in _RESERVED_KEYS})
        return json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
