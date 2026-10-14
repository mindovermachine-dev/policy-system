"""Domain-specific exception types for `ps_service.audit`.

One exception type per distinct failure boundary the audit component owns, never a generic
`Exception`/`ValueError`. Callers translate these at their own API boundary; none of the messages
carry connection internals (host, port, driver text) or SQL.

Types by boundary:

- Validation before any write: `AuditUnknownActionError`, `AuditInvalidDetailsError`.
- Store availability: `AuditPostgresUnavailableError` (cannot connect or read),
  `AuditPersistenceError` (connected, but the `INSERT` failed).
- Read input: `AuditInvalidCursorError`.
- Attribution: `AuditActorUnresolvedError`.
- Fail-closed emission: `AuditTrailUnavailableError`.
"""

from __future__ import annotations


class AuditUnknownActionError(Exception):
    """An audit row was requested for an `action` that has no registered `details` model.

    Raised by `PsycopgAuditStore.record` (and so by `record_standalone`) before any `INSERT` is
    attempted. Every action must be registered with `register_audit_action` by the component
    that emits it before it can be recorded; the caller has a wiring bug, not an outage, and
    should not retry.
    """


class AuditInvalidDetailsError(Exception):
    """An audit row's `details` failed validation against the action's registered model.

    Raised by `PsycopgAuditStore.record` (and so by `record_standalone`) before any `INSERT` is
    attempted. Wraps the underlying `pydantic.ValidationError` as `__cause__`. Covers a missing
    required field and an undeclared extra field alike, because every `AuditDetails` subclass
    forbids extra fields. The caller has a bug in the details it built and should not retry.
    """


class AuditPostgresUnavailableError(Exception):
    """The PS state Postgres instance that holds `audit_events` could not be reached.

    Raised by `PsycopgAuditStore.record_standalone` and `PsycopgAuditStore.query` when the
    connection cannot be opened (state Postgres unconfigured or a genuine connection failure),
    and by `query` when the read itself fails with a driver error. The message is a fixed,
    generic string with no host, port or driver detail (fail closed, never leak connection
    internals); the original error is chained as `__cause__`. Callers translate it into their
    own API-boundary error instead of passing connection detail across the API boundary.
    """


class AuditPersistenceError(Exception):
    """The `audit_events` `INSERT` failed after a connection was already open.

    Raised by `PsycopgAuditStore.record_standalone` when the driver raises once past the
    connection step (the connection step itself raises `AuditPostgresUnavailableError`), and
    by `PsycopgAuditStore.record` if the `INSERT ... RETURNING` yields no row. Wraps the
    driver-level `psycopg.Error` as `__cause__` when there is one.
    """


class AuditInvalidCursorError(Exception):
    """A pagination `cursor` passed to `AuditStore.query` is not a well-formed token.

    Raised by `PsycopgAuditStore.query` before any connection is opened, for any malformed
    cursor: not valid base64, the wrong field count once decoded, an unparseable timestamp, or a
    non-UUID id. The cursor is opaque to clients, so the caller should report an invalid cursor
    and ask for the first page again.
    """


class AuditActorUnresolvedError(Exception):
    """No audit actor could be resolved for an audited operation.

    Raised by `resolve_audit_actor` when the caller carries no verified identity and the
    local-test bypass is not active. Fail-closed: a missing actor is never silently attributed
    to the bypass sentinel, and the caller must reject the operation.
    """


class AuditTrailUnavailableError(Exception):
    """The opening audit row could not be written, so the operation was not performed.

    Raised by `record_opening_row` and `record_opening_row_with_event_id` when the audit store is
    down, the insert fails, or the row is rejected by its registered `details` model. The
    message is fixed and carries no store detail (host, SQL, driver text); the original store
    error is chained as `__cause__`. The caller must not run the operation and should report the
    audit trail as temporarily unavailable.
    """
