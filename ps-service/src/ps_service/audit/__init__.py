"""ps_service.audit -- package front door (issue #147).

Shared, insert-only audit trail for every PS Service component that needs to
record who did what to what, and when. Domain path: `ps.service.audit`
(`docs/architecture/ps-solution-architecture.md`).

Deliberately not nested under `ps_service.authz`, even though `audit_events`
lives in the same Postgres instance authz already owns: #134 (policy
lifecycle), #136 (supersede-fork), and #140 (`user.invite`) are not authz
concerns, and each registers its own typed `details` models against this
component's extensible action registry rather than editing this package.

This slice (Slice 1, PLAN.md/CHANGES.md Appendix A1) ships the `audit_events`
schema, the `AuditDetails` base type, the extensible action registry, and
`AuditStore.record` -- the cursor-scoped write a later slice's in-transaction
callers (#133's `store.py`, repointed in Slice 2) will use. Nothing in
`ps_service` calls this package yet.

Re-exports the store/model front door and its domain-specific errors,
matching `ps_service.authz`/`ps_service.passkey_signing`'s own package
front-door convention.
"""

from __future__ import annotations

from ps_service.audit.errors import (
    AuditInvalidCursorError,
    AuditInvalidDetailsError,
    AuditPersistenceError,
    AuditPostgresUnavailableError,
    AuditUnknownActionError,
)
from ps_service.audit.models import (
    AuditDetails,
    AuditEventRow,
    AuditQueryFilters,
    AuditQueryPage,
    is_known_resource_type,
    register_audit_action,
    register_audit_resource_type,
    resolve_details_model,
)
from ps_service.audit.store import AuditStore, PsycopgAuditStore

__all__ = [
    "AuditDetails",
    "AuditEventRow",
    "AuditInvalidCursorError",
    "AuditInvalidDetailsError",
    "AuditPersistenceError",
    "AuditPostgresUnavailableError",
    "AuditQueryFilters",
    "AuditQueryPage",
    "AuditStore",
    "AuditUnknownActionError",
    "PsycopgAuditStore",
    "is_known_resource_type",
    "register_audit_action",
    "register_audit_resource_type",
    "resolve_details_model",
]
