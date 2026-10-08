"""Domain-specific exception types for `ps_service.audit` (issue #147).

One exception type per distinct failure boundary this component owns, never
a generic `Exception`/`ValueError` (L1/L2 Error Handling) -- mirrors the
shape of every other `ps_service` component's `errors.py` module (e.g.
`ps_service.passkey_signing.errors`).

Slice 1 shipped the two `AuditStore.record`-boundary errors. Slice 3 added the
two `record_standalone`-boundary errors below (mirroring
the access-role store's own connection/persistence error split):
`AuditPostgresUnavailableError` for a connection that could not
be opened at all (AC-BI-011 -- fail closed, no host/port/driver detail in
the message), `AuditPersistenceError` for a connection that opened fine but
whose `INSERT` itself then failed. `query`'s own connectivity error (Slice 4)
reuses `AuditPostgresUnavailableError` rather than adding a third type, since
it is the same failure boundary (the PS state Postgres instance is
unreachable), just reached from a different method. Slice 4 adds
`AuditInvalidCursorError` below, `query`'s own malformed-`cursor` boundary.
"""

from __future__ import annotations


class AuditUnknownActionError(Exception):
    """`AuditStore.record` was called with an `action` that has no registered `details` model.

    Raised by :meth:`ps_service.audit.store.PsycopgAuditStore.record` before
    any `INSERT` is attempted (AC-BI-005) -- every action must be registered
    via :func:`ps_service.audit.models.register_audit_action` by the
    component that emits it before it can ever be recorded.
    """


class AuditInvalidDetailsError(Exception):
    """`AuditStore.record`'s `details` failed validation against the action's registered model.

    Raised by :meth:`ps_service.audit.store.PsycopgAuditStore.record` before
    any `INSERT` is attempted (AC-BI-005/AC-BI-009) -- wraps the underlying
    `pydantic.ValidationError`. Covers both a missing required field and an
    undeclared extra field, since every `AuditDetails` subclass sets
    `model_config = ConfigDict(extra="forbid")`.
    """


class AuditPostgresUnavailableError(Exception):
    """The PS state Postgres instance (`audit_events`'s own store) could not be reached (AC-BI-011).

    Raised by :meth:`ps_service.audit.store.PsycopgAuditStore.record_standalone`
    (and, in a later slice, `query`) when `connect_from_config` fails --
    either unconfigured (`StatePostgresConnectionError`) or a genuine
    connection failure (`psycopg.Error`). The message is a fixed, generic
    string carrying no host/port/driver detail, mirroring
    `AuthorizationStoreUnavailableError`'s own "fail closed, never leak
    connection internals" discipline (PLAN.md §6) -- callers translate this
    into that same API-boundary type rather than letting any raw connection
    detail cross the MCP boundary.
    """


class AuditPersistenceError(Exception):
    """`record_standalone`'s own `INSERT` failed after a connection was already open.

    Raised by :meth:`ps_service.audit.store.PsycopgAuditStore.record_standalone`
    when the underlying `psycopg` call raises once past the connection step
    (distinct from `AuditPostgresUnavailableError`, which covers the
    connection step itself) -- wraps the driver-level `psycopg.Error`, mirroring
    the other stores' own "wrap the driver exception" convention.
    """


class AuditInvalidCursorError(Exception):
    """`AuditStore.query`'s `cursor` argument does not decode to a well-formed pagination token.

    Raised by :meth:`ps_service.audit.store.PsycopgAuditStore.query` (issue
    #147, Slice 4) before any query runs, for any malformed `cursor`: not
    valid base64, the wrong field count once decoded, an unparseable
    timestamp, or a non-UUID id. `list-audit-events`'s own defensive
    extension of AC-BI-008's "invalid filter" contract to the one
    filter-like input AC-BI-008's own enumerated list doesn't literally name
    (PLAN.md §3.4) -- the MCP tool never documents the cursor's internal
    shape, only that it is opaque.
    """


class AuditActorUnresolvedError(Exception):
    """No audit actor could be resolved for an audited operation (issue #195, AC-BI-001).

    Raised by :func:`ps_service.audit.actor.resolve_audit_actor` when the caller carries no
    verified identity and the local-test bypass is not active. Fail-closed: a missing actor is
    never silently attributed to the bypass sentinel.
    """


class AuditTrailUnavailableError(Exception):
    """The opening audit row could not be written, so the operation was not performed.

    Raised by :func:`ps_service.audit.emit.record_opening_row` (issue #195, AC-BI-011) when the
    audit store is down, the insert fails, or the row is rejected by its registered model. The
    message is fixed and carries no store detail (host, SQL, driver text); the original store
    error is chained as `__cause__`.
    """
