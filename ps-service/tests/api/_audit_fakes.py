"""A compact in-memory `AuditStore` for `tests/api`.

Own copy of `tests/audit/_fakes.py::InMemoryAuditStore`'s write side, not a cross-package import:
`tests/api/` sorts before `tests/audit/`, and under `--import-mode=importlib` a package only becomes
importable once pytest has collected something from it, so `from audit._fakes import ...` cannot
resolve from here (the same reason `test_near_miss_tools.py` keeps its own pending-approval fake).
It validates `details` against the real action registry, records ordered `rows`/`events`, and can
be told to raise per outcome to exercise the fail-closed opening row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ps_service.audit.models import resolve_details_model

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ps_service.audit.models import AuditQueryFilters, AuditQueryPage


@dataclass(frozen=True)
class RecordedAuditRow:
    """One row written through `record_standalone`."""

    actor_subject: str
    actor_issuer: str
    action: str
    resource_type: str
    resource_id: str
    outcome: str
    details: dict[str, object]


@dataclass
class InMemoryAuditStore:
    """Registry-validating in-memory `AuditStore` (write side only)."""

    rows: list[RecordedAuditRow] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    fail_on_outcome: dict[str, Exception] = field(default_factory=dict)
    event_ids: list[str] = field(default_factory=list)
    """The id `record_standalone_with_event_id` handed out for each row, in order."""
    event_id_sink: Callable[[str], None] | None = None
    """Called with each new event id (a test's way to register it with the graph log)."""

    def record(self, cur: object, **kwargs: object) -> str:
        del cur, kwargs
        raise NotImplementedError

    def query(
        self, *, filters: AuditQueryFilters, cursor: str | None, page_size: int
    ) -> AuditQueryPage:
        del filters, cursor, page_size
        raise NotImplementedError

    def record_standalone(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        details: Mapping[str, object],
    ) -> None:
        self.record_standalone_with_event_id(
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            details=details,
        )

    def record_standalone_with_event_id(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        details: Mapping[str, object],
    ) -> str:
        """Record the row as `record_standalone` does and return its fresh event id.

        The id is appended to `event_ids` and handed to `event_id_sink`, which a test points at the
        graph log's committed audit rows so a group anchored to this row is accepted.
        """
        error = self.fail_on_outcome.get(outcome)
        if error is not None:
            raise error
        model = resolve_details_model(action)
        assert model is not None, f"unregistered audit action {action!r}"
        validated = model.model_validate(dict(details))
        event_id = str(uuid.uuid4())
        self.event_ids.append(event_id)
        if self.event_id_sink is not None:
            self.event_id_sink(event_id)
        self.events.append(f"audit:{action}:{outcome}")
        self.rows.append(
            RecordedAuditRow(
                actor_subject=actor_subject,
                actor_issuer=actor_issuer,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=outcome,
                details=validated.model_dump(mode="json", exclude_none=True),
            )
        )
        return event_id
