"""Shared in-memory `AuditStore` double for emission tests (issue #195).

`InMemoryAuditStore.record_standalone` validates `details` against the real action registry
(so a test fails if an emitter writes an undeclared field), appends to `rows` and to the
ordered `events` list a test may share with a transport/graph fake to assert
audit-before-effect. `fail_on_outcome` / `fail_on_action` raise a chosen exception instead of
writing, to exercise the fail-closed opening row and the best-effort terminal row.

Import as `from audit._fakes import InMemoryAuditStore` (the cross-package idiom of
`tests/ingestion_runs/_fakes.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ps_service.audit.models import (
    AuditEventRow,
    AuditQueryFilters,
    AuditQueryPage,
    resolve_details_model,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


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
    """Registry-validating in-memory `AuditStore`."""

    rows: list[RecordedAuditRow] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    fail_on_outcome: dict[str, Exception] = field(default_factory=dict)
    fail_on_action: dict[str, Exception] = field(default_factory=dict)
    history: list[AuditEventRow] = field(default_factory=list)

    def record(self, cur: object, **kwargs: object) -> None:
        del cur, kwargs
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
        error = self.fail_on_action.get(action) or self.fail_on_outcome.get(outcome)
        if error is not None:
            raise error
        model = resolve_details_model(action)
        assert model is not None, f"unregistered audit action {action!r}"
        validated = model.model_validate(dict(details))
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

    def query(
        self, *, filters: AuditQueryFilters, cursor: str | None, page_size: int
    ) -> AuditQueryPage:
        """Page over `rows` newest-first, filtered by action, resource, actor and `details`."""
        del cursor
        matching = [
            row
            for row in reversed(self.rows)
            if (filters.action is None or row.action == filters.action)
            and (filters.resource_type is None or row.resource_type == filters.resource_type)
            and (filters.resource_id is None or row.resource_id == filters.resource_id)
            and (filters.actor_subject is None or row.actor_subject == filters.actor_subject)
            and all(row.details.get(key) == value for key, value in (filters.details or {}).items())
        ]
        events = tuple(
            AuditEventRow(
                id=f"row-{index}",
                occurred_at=_EPOCH,
                actor_subject=row.actor_subject,
                actor_issuer=row.actor_issuer,
                action=row.action,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                outcome=row.outcome,  # pyright: ignore[reportArgumentType]
                details=row.details,
            )
            for index, row in enumerate(matching[:page_size])
        )
        return AuditQueryPage(events=events, next_cursor=None)


def audit_store_factory(store: InMemoryAuditStore) -> Callable[[object], InMemoryAuditStore]:
    """A `PsycopgAuditStore(config)`-shaped factory always returning `store` (for `monkeypatch`)."""

    def _factory(_config: object) -> InMemoryAuditStore:
        return store

    return _factory
