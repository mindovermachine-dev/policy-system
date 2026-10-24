"""Audit lifecycle primitives shared by every emitting component.

Conventions (the audit trail's row-shape contract):

- Outcome vs lifecycle. `audit_events.outcome` is `applied | rejected | failed` and means whether
  the effect happened. Lifecycle is carried in `details.status` (`started`, then `succeeded` or
  `failed`). "Started" or "completed" means `outcome='applied'` plus that `details.status`.
- Row shape per action. Long-running operations (ingestion, restore) write an opening row, then a
  terminal row. Short atomic operations (near-miss resolve, invite) write an `applied` row before
  the effect and a `failed` row after it if the effect fails.
- Failure policy. The opening row is FAIL-CLOSED (:func:`record_opening_row`): if it cannot be
  written the operation does not run. A terminal or follow-up row is BEST-EFFORT
  (:func:`record_follow_up_row`): a write failure is logged with the run id and the caller's
  result is unchanged. Asynchronous runs have a reconciler for a missing terminal row; synchronous
  runs do not (known limitation).
- Actor. The principal that performed the operation (for a passkey-approved merge, the approver,
  with the `approval_id` in the details). Under the local-test bypass it is
  `system:local-test-bypass` (:mod:`ps_service.audit.actor`). A sweep's re-ingests are attributed
  to the caller who triggered the sweep.
- `resource_id` is the thing acted on (an instrument id, a review id, an invitee email). Only
  `ingestion_run.*` keeps its run id as `resource_id`.
- Never record a secret (invite token, URL, credential), free-text error or stack trace; use an
  enumerated `reason_code`.
- Synchronous vs asynchronous ingestion. A synchronous ingest writes its opening row before the
  identity check, so a request rejected by it (unknown CELEX, short-name collision, already
  ingested) is always followed by a terminal row (`failed` with a `reason_code`, or `succeeded`
  with `outcome='already_ingested'`). An asynchronous `start-ingestion` rejected before its run
  exists writes no row. Polling a run (`get-ingestion-status`) writes nothing, except the
  reconciler's single terminal row for an orphaned run.
- `check-regulations`. One pair per re-ingest that actually runs (`trigger='amendment_check'`,
  the re-ingest's own run id as `resource_id`, the sweep caller as actor); a call that runs no
  pipeline stage (`already_processed`, or a link-only completion) writes no row. The sweep runs
  the full UC-4 pipeline (Ingestion, Domain Mapper, Company Merge), so the completion row carries
  the real `new_obligations`, `new_capabilities` and `matched_capabilities` from the merge result;
  a failed run carries its `reason_code`. An unwritable opening row aborts the whole sweep;
  pairs already written stay.
- Passkey-approved merge. A near-miss Capability merge is a graph-cleanup `merge-capabilities`
  approval, so it writes a `capability.merge` row (not `near_miss.resolve`) when the approver
  signs, with the approver as actor, the review id and before/after snapshots. If that row cannot
  be written the merge does not run and the single-use approval is already consumed, so a new
  approval is needed.
- `access.gate`. A verified caller denied by a request gate (an MCP tool or a REST route) gets one
  best-effort row: outcome `rejected`, the denied principal as actor, resource type `operation`,
  the gated operation's name as `resource_id` and only the required role in the details. A denial
  of a caller with no verified identity, and the internal role checks (the sign-time re-check, the
  service-layer assertions), write no `access.gate` row; `grant` and `revoke` keep their own
  `access_role.*` `rejected` row and no second one.
- `passkey.enroll`. One best-effort row after a passkey is stored: outcome `applied`, the actor is
  the `(sub, iss)` bound to the pending approval (the caller is identified by the approval code, not
  by authentication, hence `proof='approval_code'` in the details), resource type
  `pending_approval`. A rejected ceremony step writes no row and a signed action writes no
  `passkey.sign` row: the action's own effect row records the signing.
- `query.cypher`. One best-effort row per `run-cypher` tool call, written by the tool and never by
  the query engine or another in-process caller: outcome `applied`, `rejected` (a write clause) or
  `failed`, details limited to the row count, the truncation flag and an enumerated `reason_code`,
  never the query text or a database error text. Under the local-test bypass the actor is the
  bypass sentinel.
- Reading. `list-audit-events` returns these rows; its `details` filter (celex,
  regulatory_instrument_id, instrument_id; see `AUDIT_DETAILS_FILTER_KEYS`) answers "who ingested
  or restored X".

Failure logs pick up the run id bound by `bind_run_context` (the request or worker run); they
carry exception CLASS NAMES only, never an exception message, so no store detail leaks.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ps_service.audit.errors import (
    AuditInvalidDetailsError,
    AuditPersistenceError,
    AuditPostgresUnavailableError,
    AuditTrailUnavailableError,
    AuditUnknownActionError,
)
from ps_service.audit.store import EventIdAuditStore
from ps_service.logging.errors import LoggingLifecycleError
from ps_service.logging.facade import emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ps_service.audit.store import AuditStore
    from ps_service.logging.emitter import LogEmitter

_AUDIT_ERRORS = (
    AuditPostgresUnavailableError,
    AuditPersistenceError,
    AuditUnknownActionError,
    AuditInvalidDetailsError,
)
_UNAVAILABLE_MESSAGE = (
    "The audit trail is temporarily unavailable; the operation was not performed."
)


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Who is acting and where their audit rows go: one value passed through orchestration.

    Attributes:
        actor: The `(subject, issuer)` pair every row is attributed to, as returned by
            `resolve_audit_actor`.
        store: The `AuditStore` the rows are written to.
    """

    actor: tuple[str, str]
    store: AuditStore


@dataclass(frozen=True, slots=True)
class AuditTarget:
    """What a row is about: the registered `action` and the resource acted on.

    Attributes:
        action: A registered, namespaced audit action string.
        resource_type: The kind of resource acted on (a registered resource type).
        resource_id: The identifier of the resource acted on; never a secret.
        log_resource_id: Whether `resource_id` may appear in failure log lines. Set it to
            `False` when the id is personal data (for example an invitee email): the audit row
            records it, the log must not. Defaults to `True`.
    """

    action: str
    resource_type: str
    resource_id: str
    log_resource_id: bool = True


def _log_failure(
    *,
    log_action: str,
    component: str,
    target: AuditTarget,
    error: Exception,
    emitter: LogEmitter | None,
) -> None:
    """Log an audit-write failure (error class name only); ignore a logging-lifecycle error."""
    with contextlib.suppress(LoggingLifecycleError):
        emit_log_entry(
            component=component,
            action=log_action,
            entity_id=target.resource_id if target.log_resource_id else None,
            outcome="failed",
            extra={"audit_action": target.action, "reason": type(error).__name__},
            emitter=emitter,
        )


def record_opening_row(
    audit: AuditContext,
    target: AuditTarget,
    *,
    component: str,
    details: Mapping[str, object],
    emitter: LogEmitter | None = None,
) -> None:
    """Write the `applied` opening row of an operation; FAIL-CLOSED.

    Call this before the operation's effect. If the row cannot be written the operation must not
    run, so every effect has an audit row that precedes it.

    Args:
        audit: The acting principal and the store to write to.
        target: The registered action and the resource acted on.
        component: Component name stamped on the failure log entry if the write fails.
        details: The action's `details` payload; validated against the action's registered
            model by the store. Must not contain secrets, free-text errors or stack traces.
        emitter: Log emitter for the failure log entry; `None` uses the process default.

    Returns:
        None.

    Raises:
        AuditTrailUnavailableError: the store is unreachable, the insert failed, the action is
            not registered or `details` fails its model. The caller must not run the
            operation. The store error is chained as `__cause__`.

    Side Effects:
        Inserts one `audit_events` row with `outcome='applied'` in its own transaction. On
        failure, emits one `failed` log entry carrying the audit action and the error class name
        (and `target.resource_id` unless `target.log_resource_id` is `False`).
    """
    try:
        audit.store.record_standalone(
            actor_subject=audit.actor[0],
            actor_issuer=audit.actor[1],
            action=target.action,
            resource_type=target.resource_type,
            resource_id=target.resource_id,
            outcome="applied",
            details=details,
        )
    except _AUDIT_ERRORS as exc:
        _log_failure(
            log_action="audit_opening_failed",
            component=component,
            target=target,
            error=exc,
            emitter=emitter,
        )
        raise AuditTrailUnavailableError(_UNAVAILABLE_MESSAGE) from exc


def record_opening_row_with_event_id(
    audit: AuditContext,
    target: AuditTarget,
    *,
    component: str,
    details: Mapping[str, object],
    emitter: LogEmitter | None = None,
) -> str:
    """Write the `applied` opening row of an operation and return the new row's id; FAIL-CLOSED.

    Use it for an operation whose graph write is anchored to the audit row that opened it: the
    returned id is stored with the write so the two can be correlated.

    Args:
        audit: The acting principal and the store to write to. `audit.store` must be an
            `EventIdAuditStore`.
        target: The registered action and the resource acted on.
        component: Component name stamped on the failure log entry if the write fails.
        details: The action's `details` payload; validated against the action's registered
            model by the store. Must not contain secrets, free-text errors or stack traces.
        emitter: Log emitter for the failure log entry; `None` uses the process default.

    Returns:
        The generated id (a UUID string) of the inserted `audit_events` row.

    Raises:
        AuditTrailUnavailableError: the store is unreachable, the insert failed, the action is
            not registered or `details` fails its model. The caller must not run the
            operation. The store error is chained as `__cause__`.
        TypeError: `audit.store` cannot report the id of the row it wrote (a wiring error, not an
            outage).

    Side Effects:
        Inserts one `audit_events` row with `outcome='applied'` in its own transaction. On
        failure, emits one `failed` log entry carrying the audit action and the error class name
        (and `target.resource_id` unless `target.log_resource_id` is `False`).
    """
    store = audit.store
    if not isinstance(store, EventIdAuditStore):
        message = f"{type(store).__name__} cannot report an audit event id"
        raise TypeError(message)
    try:
        return store.record_standalone_with_event_id(
            actor_subject=audit.actor[0],
            actor_issuer=audit.actor[1],
            action=target.action,
            resource_type=target.resource_type,
            resource_id=target.resource_id,
            outcome="applied",
            details=details,
        )
    except _AUDIT_ERRORS as exc:
        _log_failure(
            log_action="audit_opening_failed",
            component=component,
            target=target,
            error=exc,
            emitter=emitter,
        )
        raise AuditTrailUnavailableError(_UNAVAILABLE_MESSAGE) from exc


def record_follow_up_row(
    audit: AuditContext,
    target: AuditTarget,
    *,
    component: str,
    outcome: Literal["applied", "rejected", "failed"],
    details: Mapping[str, object],
    emitter: LogEmitter | None = None,
) -> bool:
    """Write a terminal or follow-up row after an operation's effect; BEST-EFFORT.

    A failure to write is logged and swallowed, so the caller's result is never changed by an
    audit outage.

    Args:
        audit: The acting principal and the store to write to.
        target: The registered action and the resource acted on.
        component: Component name stamped on the failure log entry if the write fails.
        outcome: Whether the effect happened: `"applied"`, `"rejected"` or `"failed"`.
        details: The action's `details` payload; validated against the action's registered
            model by the store. Must not contain secrets, free-text errors or stack traces.
        emitter: Log emitter for the failure log entry; `None` uses the process default.

    Returns:
        `True` when the row was written; `False` when the store failed (logged).

    Raises:
        Nothing from the audit store: store, unknown-action and invalid-details errors are
        caught and reported as `False`.

    Side Effects:
        Inserts one `audit_events` row with the given `outcome` in its own transaction. On
        failure, emits one `failed` log entry carrying the audit action and the error class name
        (and `target.resource_id` unless `target.log_resource_id` is `False`).
    """
    try:
        audit.store.record_standalone(
            actor_subject=audit.actor[0],
            actor_issuer=audit.actor[1],
            action=target.action,
            resource_type=target.resource_type,
            resource_id=target.resource_id,
            outcome=outcome,
            details=details,
        )
    except _AUDIT_ERRORS as exc:
        _log_failure(
            log_action="audit_terminal_failed",
            component=component,
            target=target,
            error=exc,
            emitter=emitter,
        )
        return False
    return True
