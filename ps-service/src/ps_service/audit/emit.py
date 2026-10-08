"""Audit lifecycle primitives shared by every emitting component (issue #195).

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
  with `outcome='already_ingested'`). An asynchronous `start_ingestion` rejected before its run
  exists writes no row. Polling a run (`get_ingestion_status`) writes nothing, except the
  reconciler's single terminal row for an orphaned run.
- `check_regulations`. One pair per re-ingest that actually runs (`trigger='amendment_check'`,
  the re-ingest's own run id as `resource_id`, the sweep caller as actor); `resume` and
  `already_processed` outcomes ingest nothing and write no row. The counts are 0/0/0 because the
  sweep re-runs only the Ingestion stage (GitHub issue #201 tracks the UC-4 doc/code gap). An
  unwritable opening row aborts the whole sweep; pairs already written stay.
- Passkey-approved merge. The `near_miss.resolve` row is written when the approver signs, with
  the approver as actor and the `approval_id`. If its opening row cannot be written the merge
  does not run and the single-use approval is already consumed, so a new approval is needed.
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
    """Who is acting and where their audit rows go: one value passed through orchestration."""

    actor: tuple[str, str]
    store: AuditStore


@dataclass(frozen=True, slots=True)
class AuditTarget:
    """What a row is about: the registered `action` and the `resource` acted on.

    `log_resource_id=False` keeps `resource_id` out of failure log lines (set it when the id is
    personal data, e.g. an invitee email: the audit row records it, the log must not).
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
    """Write the `applied` opening row; FAIL-CLOSED (AC-BI-011).

    Raises:
        AuditTrailUnavailableError: the row could not be written; the caller must not run the
            operation. The store error is chained as `__cause__`.
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


def record_follow_up_row(
    audit: AuditContext,
    target: AuditTarget,
    *,
    component: str,
    outcome: Literal["applied", "rejected", "failed"],
    details: Mapping[str, object],
    emitter: LogEmitter | None = None,
) -> bool:
    """Write a terminal or follow-up row; BEST-EFFORT (AC-BI-015). Never raises.

    Returns:
        True when the row was written; False when the store failed (logged with the bound run id).
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
