"""ps_service.audit -- the shared, insert-only audit trail.

Records who did what to what, and when, for every PS Service component that needs it. Domain
path: `ps.service.audit` (`docs/architecture/ps-solution-architecture.md`).

The component is deliberately standalone, not nested under any one consumer, even though
`audit_events` lives in the PS state Postgres instance that several components share: audit is
not an access-control concern, and each emitting component registers its own typed `details`
model against this component's extensible action registry instead of editing this package.

Public surface, re-exported here:

- Store: `AuditStore` (Protocol), `EventIdAuditStore` (Protocol for stores that report the id of
  the row they wrote) and `PsycopgAuditStore` (PostgreSQL implementation).
- Models and registries: `AuditDetails`, `AuditEventRow`, `AuditQueryFilters`, `AuditQueryPage`,
  `register_audit_action`, `resolve_details_model`, `register_audit_resource_type`,
  `is_known_resource_type`, `AUDIT_DETAILS_FILTER_KEYS`.
- Emission helpers: `AuditContext`, `AuditTarget`, `record_opening_row`,
  `record_opening_row_with_event_id`, `record_follow_up_row`; the row-shape contract (outcome vs
  lifecycle, fail-closed opening row, best-effort terminal row, actor semantics, `resource_id`
  convention) is documented in the `ps_service.audit.emit` module docstring.
- Attribution: `resolve_audit_actor`, `LOCAL_TEST_BYPASS_AUDIT_ACTOR`.
- Errors: `AuditActorUnresolvedError`, `AuditInvalidCursorError`, `AuditInvalidDetailsError`,
  `AuditPersistenceError`, `AuditPostgresUnavailableError`, `AuditTrailUnavailableError`,
  `AuditUnknownActionError`.
- `MIGRATIONS_DIR`: the component's own migration directory.

Insert-only is enforced by omission: the store interface defines no `UPDATE` or `DELETE`.
"""

from __future__ import annotations

from pathlib import Path

from ps_service.audit.actor import LOCAL_TEST_BYPASS_AUDIT_ACTOR, resolve_audit_actor
from ps_service.audit.emit import (
    AuditContext,
    AuditTarget,
    record_follow_up_row,
    record_opening_row,
    record_opening_row_with_event_id,
)
from ps_service.audit.errors import (
    AuditActorUnresolvedError,
    AuditInvalidCursorError,
    AuditInvalidDetailsError,
    AuditPersistenceError,
    AuditPostgresUnavailableError,
    AuditTrailUnavailableError,
    AuditUnknownActionError,
)
from ps_service.audit.models import (
    AUDIT_DETAILS_FILTER_KEYS,
    AuditDetails,
    AuditEventRow,
    AuditQueryFilters,
    AuditQueryPage,
    is_known_resource_type,
    register_audit_action,
    register_audit_resource_type,
    resolve_details_model,
)
from ps_service.audit.store import AuditStore, EventIdAuditStore, PsycopgAuditStore

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
"""This component's own migration directory, listed by the composition root (`ps_service.main`)."""

__all__ = [
    "AUDIT_DETAILS_FILTER_KEYS",
    "LOCAL_TEST_BYPASS_AUDIT_ACTOR",
    "MIGRATIONS_DIR",
    "AuditActorUnresolvedError",
    "AuditContext",
    "AuditDetails",
    "AuditEventRow",
    "AuditInvalidCursorError",
    "AuditInvalidDetailsError",
    "AuditPersistenceError",
    "AuditPostgresUnavailableError",
    "AuditQueryFilters",
    "AuditQueryPage",
    "AuditStore",
    "AuditTarget",
    "AuditTrailUnavailableError",
    "AuditUnknownActionError",
    "EventIdAuditStore",
    "PsycopgAuditStore",
    "is_known_resource_type",
    "record_follow_up_row",
    "record_opening_row",
    "record_opening_row_with_event_id",
    "register_audit_action",
    "register_audit_resource_type",
    "resolve_audit_actor",
    "resolve_details_model",
]
