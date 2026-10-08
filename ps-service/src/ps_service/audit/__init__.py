"""ps_service.audit -- package front door (issue #147).

Shared, insert-only audit trail for every PS Service component that needs to
record who did what to what, and when. Domain path: `ps.service.audit`
(`docs/architecture/ps-solution-architecture.md`).

Deliberately a standalone component, not nested under any one consumer, even
though `audit_events` lives in the PS state Postgres instance that several
components share: #134 (policy lifecycle), #136 (supersede-fork), and #140
(`user.invite`) are not access-control concerns, and each registers its own
typed `details` models against this component's extensible action registry
rather than editing this package.

This slice (Slice 1, PLAN.md/CHANGES.md Appendix A1) ships the `audit_events`
schema, the `AuditDetails` base type, the extensible action registry, and
`AuditStore.record` -- the cursor-scoped write a later slice's in-transaction
callers (#133's `store.py`, repointed in Slice 2) will use. Nothing in
`ps_service` calls this package yet.

Emission convention (issue #195): see :mod:`ps_service.audit.emit` for the outcome-vs-lifecycle
rule, the row shape per action, the failure policy (opening row fail-closed, terminal row
best-effort), the actor semantics and the `resource_id` convention.

Re-exports the store/model front door and its domain-specific errors,
matching `ps_service.passkey_signing`'s own package
front-door convention.
"""

from __future__ import annotations

from pathlib import Path

from ps_service.audit.actor import LOCAL_TEST_BYPASS_AUDIT_ACTOR, resolve_audit_actor
from ps_service.audit.emit import (
    AuditContext,
    AuditTarget,
    record_follow_up_row,
    record_opening_row,
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
from ps_service.audit.store import AuditStore, PsycopgAuditStore

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
    "PsycopgAuditStore",
    "is_known_resource_type",
    "record_follow_up_row",
    "record_opening_row",
    "register_audit_action",
    "register_audit_resource_type",
    "resolve_audit_actor",
    "resolve_details_model",
]
