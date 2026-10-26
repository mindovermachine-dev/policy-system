"""Revoke a principal's signing passkeys: void its pending approvals, delete its credentials.

Used by the owner passkey reset (`python -m ps_service.passkey_signing.revoke`): after the owner
lost a device, every pending approval of the owner's `(subject, issuer)` is voided and every
signing credential of it is deleted, under every relying-party id, so the lost device can no
longer sign an approval and no outstanding approval code can enrol a credential, sign or execute.
The next approval then asks the owner to enrol a new signing passkey.

Voiding consumes the approval the way a refused signature does: status `signed` with a fixed
`error` outcome telling the requester to ask for a new approval. Every ceremony route rejects a
non-pending approval, and expired pending approvals are voided too, so after a revocation the
principal has no pending approval left.

`SigningRevocationStore` is the persistence seam; `PsycopgSigningRevocationStore` implements it on
the passkey-signing Postgres (`pending_approvals`, `signing_credentials`), voiding and deleting in
one transaction. Every statement it runs is one of `ACTOR_SCOPED_STATEMENTS`, each scoped by
`actor_subject` AND `actor_issuer`, so a revocation can never reach another principal, nor the
same subject at another issuer.

Audit: the `passkey.revoke` row lives in the state database, the credentials in the signing
database, so no single transaction spans both. The `applied` row is written fail-closed inside
the signing transaction, before its COMMIT: if the row cannot be written nothing changes. If
the COMMIT then fails, a best-effort `failed` row with `reason_code` follows. Only when that row
also cannot be written does an `applied` row stand without its effect; a re-run completes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, cast

import psycopg
from psycopg.types.json import Json

from ps_service.audit.emit import (
    AuditContext,
    AuditTarget,
    record_follow_up_row,
    record_opening_row,
)
from ps_service.passkey_signing.audit_actions import (
    PASSKEY_REVOKE_ACTION,
    PRINCIPAL_RESOURCE_TYPE,
)
from ps_service.passkey_signing.errors import SigningRevocationPersistenceError
from ps_service.passkey_signing.store import connect_from_config

if TYPE_CHECKING:
    from collections.abc import Callable

    from psycopg.rows import TupleRow

    from ps_service.audit.store import AuditStore
    from ps_service.config import ServiceConfig
    from ps_service.logging.emitter import LogEmitter

__all__ = [
    "ACTOR_SCOPED_STATEMENTS",
    "PASSKEY_RESET_ACTOR",
    "ActorSigningRevocation",
    "ActorSigningState",
    "PsycopgSigningRevocationStore",
    "SigningRevocationStore",
    "read_actor_signing_state",
    "revoke_actor_signing",
]

PASSKEY_RESET_ACTOR: Final = ("system:owner-passkey-reset", "system")
"""The `(subject, issuer)` the `passkey.revoke` row is attributed to: the operator's reset."""

_COMPONENT = "passkey_signing"

_PASSKEY_RESET_VOID_MESSAGE: Final = (
    "this approval was cancelled because the approver's signing passkeys were reset; "
    "ask for a new approval"
)

_VOID_PENDING_FOR_ACTOR: Final = """
UPDATE pending_approvals SET status = 'signed', outcome = %(outcome)s
WHERE actor_subject = %(actor_subject)s AND actor_issuer = %(actor_issuer)s
  AND status = 'pending'
RETURNING id
"""
_DELETE_CREDENTIALS_FOR_ACTOR: Final = """
DELETE FROM signing_credentials
WHERE actor_subject = %(actor_subject)s AND actor_issuer = %(actor_issuer)s
RETURNING id
"""
_COUNT_CREDENTIALS_FOR_ACTOR: Final = """
SELECT count(*) FROM signing_credentials
WHERE actor_subject = %(actor_subject)s AND actor_issuer = %(actor_issuer)s
"""
_SELECT_PENDING_IDS_FOR_ACTOR: Final = """
SELECT id FROM pending_approvals
WHERE actor_subject = %(actor_subject)s AND actor_issuer = %(actor_issuer)s
  AND status = 'pending'
ORDER BY created_at
"""

ACTOR_SCOPED_STATEMENTS: Final = (
    _VOID_PENDING_FOR_ACTOR,
    _DELETE_CREDENTIALS_FOR_ACTOR,
    _COUNT_CREDENTIALS_FOR_ACTOR,
    _SELECT_PENDING_IDS_FOR_ACTOR,
)
"""Every statement the revocation store runs; each is scoped by subject AND issuer."""


@dataclass(frozen=True, slots=True)
class ActorSigningState:
    """A read-only snapshot of one principal's signing state.

    Attributes:
        credentials: How many signing credentials the principal has, every relying party
            included.
        pending_approval_ids: The ids of the principal's pending approvals, oldest first.
    """

    credentials: int
    pending_approval_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ActorSigningRevocation:
    """What one revocation changed.

    Attributes:
        credentials_deleted: How many signing credentials were deleted.
        voided_approval_ids: The ids of the pending approvals the revocation voided.
    """

    credentials_deleted: int
    voided_approval_ids: tuple[str, ...]


class SigningRevocationStore(Protocol):
    """Persistence seam for revoking one principal's signing passkeys.

    Constructor-injected into `revoke_actor_signing` and `read_actor_signing_state`, so a test
    can substitute an in-memory fake. Every method raises `SigningRevocationPersistenceError`
    when the database call fails.
    """

    def state_for_actor(self, *, actor_subject: str, actor_issuer: str) -> ActorSigningState:
        """Count the principal's signing credentials and list its pending approvals.

        Args:
            actor_subject: Identity subject of the principal.
            actor_issuer: Identity issuer of that subject.

        Returns:
            The principal's `ActorSigningState`.

        Raises:
            SigningRevocationPersistenceError: the read failed or the connection could not be
                opened.

        Side Effects:
            Reads only; writes nothing.
        """
        ...

    def revoke_for_actor(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        void_outcome: dict[str, object],
        before_commit: Callable[[ActorSigningRevocation], None],
    ) -> ActorSigningRevocation:
        """Void the principal's pending approvals and delete its credentials, in one transaction.

        Args:
            actor_subject: Identity subject of the principal.
            actor_issuer: Identity issuer of that subject.
            void_outcome: The outcome stored on every voided approval.
            before_commit: Called with the result after the changes and before COMMIT; any
                exception it raises rolls the transaction back and propagates unchanged.

        Returns:
            What the committed transaction changed.

        Raises:
            SigningRevocationPersistenceError: a statement or the COMMIT failed, or the
                connection could not be opened; nothing was changed.

        Side Effects:
            Marks every `pending` approval of the principal `signed` with `void_outcome`,
            deletes the principal's `signing_credentials` rows and commits.
        """
        ...


class PsycopgSigningRevocationStore:
    """`SigningRevocationStore` on the passkey-signing Postgres via `psycopg`.

    Each call opens, uses and closes one bounded connection (the configured connect and
    statement timeouts apply) and runs only statements from `ACTOR_SCOPED_STATEMENTS`.
    """

    def __init__(
        self,
        config: ServiceConfig,
        *,
        connect: Callable[[], psycopg.Connection[TupleRow]] | None = None,
    ) -> None:
        """Keep how to connect; opens no connection.

        Args:
            config: Service configuration holding the `passkey_signing_postgres_*` settings.
            connect: Opens a new connection; `None` uses `connect_from_config(config)`.
        """
        self._connect: Callable[[], psycopg.Connection[TupleRow]] = (
            connect if connect is not None else lambda: connect_from_config(config)
        )

    def state_for_actor(self, *, actor_subject: str, actor_issuer: str) -> ActorSigningState:
        """Count the principal's signing credentials and list its pending approvals.

        Args:
            actor_subject: Identity subject of the principal.
            actor_issuer: Identity issuer of that subject.

        Returns:
            The principal's `ActorSigningState`.

        Raises:
            SigningRevocationPersistenceError: the read failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection and reads; writes nothing.
        """
        params = {"actor_subject": actor_subject, "actor_issuer": actor_issuer}
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(_COUNT_CREDENTIALS_FOR_ACTOR, params)
                count_row = cur.fetchone()
                cur.execute(_SELECT_PENDING_IDS_FOR_ACTOR, params)
                pending_rows = cur.fetchall()
        except psycopg.Error as exc:
            message = "failed to read the actor's signing state"
            raise SigningRevocationPersistenceError(message) from exc
        credentials = cast("int", count_row[0]) if count_row is not None else 0
        return ActorSigningState(
            credentials=credentials,
            pending_approval_ids=tuple(str(row[0]) for row in pending_rows),
        )

    def revoke_for_actor(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        void_outcome: dict[str, object],
        before_commit: Callable[[ActorSigningRevocation], None],
    ) -> ActorSigningRevocation:
        """Void the principal's pending approvals and delete its credentials, in one transaction.

        The approvals are voided first, so no approval code can enrol a credential between the
        two statements. Every pending approval is voided, expired ones too, and every
        relying-party id's credentials are deleted, the empty one too. `before_commit` runs on
        the open transaction; the changes are committed only when it returns.

        Args:
            actor_subject: Identity subject of the principal.
            actor_issuer: Identity issuer of that subject.
            void_outcome: The outcome stored on every voided approval.
            before_commit: Called with the result after the changes and before COMMIT; any
                exception it raises rolls the transaction back and propagates unchanged.

        Returns:
            What the committed transaction changed.

        Raises:
            SigningRevocationPersistenceError: a statement or the COMMIT failed, or the
                connection could not be opened; nothing was changed.

        Side Effects:
            Opens a connection, marks the principal's `pending` approvals `signed` with
            `void_outcome`, deletes its `signing_credentials` rows and commits.
        """
        params = {"actor_subject": actor_subject, "actor_issuer": actor_issuer}
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(_VOID_PENDING_FOR_ACTOR, {**params, "outcome": Json(void_outcome)})
                    voided = tuple(str(row[0]) for row in cur.fetchall())
                    cur.execute(_DELETE_CREDENTIALS_FOR_ACTOR, params)
                    deleted = len(cur.fetchall())
                result = ActorSigningRevocation(
                    credentials_deleted=deleted, voided_approval_ids=voided
                )
                before_commit(result)
                conn.commit()
        except psycopg.Error as exc:
            message = "failed to revoke the actor's signing passkeys"
            raise SigningRevocationPersistenceError(message) from exc
        return result


def _details(actor_issuer: str, result: ActorSigningRevocation) -> dict[str, object]:
    return {
        "revoked_issuer": actor_issuer,
        "credentials_deleted": result.credentials_deleted,
        "voided_approval_ids": list(result.voided_approval_ids),
    }


def revoke_actor_signing(
    *,
    actor_subject: str,
    actor_issuer: str,
    revocation_store: SigningRevocationStore,
    audit_store: AuditStore,
    emitter: LogEmitter | None = None,
) -> ActorSigningRevocation:
    """Revoke the principal's signing passkeys and write its `passkey.revoke` audit row.

    Every pending approval of the principal is voided with an outcome telling the requester to
    ask for a new approval, and every signing credential of it is deleted. The `applied` row
    (system actor `PASSKEY_RESET_ACTOR`, resource type `principal`, the subject as resource id,
    the voided approval ids in its details) is written before the signing transaction commits;
    a principal with nothing to revoke still gets a row with zero counts. The subject is
    personal data, so it never appears in a failure log line.

    Args:
        actor_subject: Identity subject of the principal whose passkeys are revoked.
        actor_issuer: Identity issuer of that subject.
        revocation_store: Where the signing credentials live.
        audit_store: Where the audit row is written.
        emitter: Log emitter for an audit-write failure entry; `None` uses the process default.

    Returns:
        What the revocation changed.

    Raises:
        AuditTrailUnavailableError: the `applied` row could not be written; nothing changed.
        SigningRevocationPersistenceError: the signing database failed. When it failed after
            the `applied` row was written (the COMMIT), a best-effort `failed` row with
            `reason_code='signing_store_unavailable'` was written first.

    Side Effects:
        Voids the principal's pending approvals, deletes its signing credentials and writes one
        or two `audit_events` rows.
    """
    audit = AuditContext(actor=PASSKEY_RESET_ACTOR, store=audit_store)
    target = AuditTarget(
        action=PASSKEY_REVOKE_ACTION,
        resource_type=PRINCIPAL_RESOURCE_TYPE,
        resource_id=actor_subject,
        log_resource_id=False,
    )
    recorded: list[ActorSigningRevocation] = []

    def record_before_commit(result: ActorSigningRevocation) -> None:
        record_opening_row(
            audit,
            target,
            component=_COMPONENT,
            details=_details(actor_issuer, result),
            emitter=emitter,
        )
        recorded.append(result)

    try:
        return revocation_store.revoke_for_actor(
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            void_outcome={"error": _PASSKEY_RESET_VOID_MESSAGE},
            before_commit=record_before_commit,
        )
    except SigningRevocationPersistenceError:
        if recorded:
            record_follow_up_row(
                audit,
                target,
                component=_COMPONENT,
                outcome="failed",
                details={
                    **_details(actor_issuer, recorded[-1]),
                    "reason_code": "signing_store_unavailable",
                },
                emitter=emitter,
            )
        raise


def read_actor_signing_state(
    *, actor_subject: str, actor_issuer: str, revocation_store: SigningRevocationStore
) -> ActorSigningState:
    """Read how many signing credentials and pending approvals the principal has.

    Args:
        actor_subject: Identity subject of the principal.
        actor_issuer: Identity issuer of that subject.
        revocation_store: Where the signing credentials live.

    Returns:
        The principal's `ActorSigningState`.

    Raises:
        SigningRevocationPersistenceError: the read failed.

    Side Effects:
        Reads only; writes nothing and records no audit row.
    """
    return revocation_store.state_for_actor(actor_subject=actor_subject, actor_issuer=actor_issuer)
