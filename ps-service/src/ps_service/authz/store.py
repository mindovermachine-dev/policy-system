"""PostgreSQL persistence for `access_role_assignments` and its audit trail.

`AccessRoleStore` is the `Protocol` that `ps_service.authz.service` depends on;
`PsycopgAccessRoleStore` is the real implementation, shaped like
`ps_service.passkey_signing.store`, with its own tables, configuration surface and
migration-tracking table. One deliberate difference: every method fails closed (raises
`StatePostgresConnectionError`) when `config.state_postgres_host` is unset, rather than
doing nothing, because every caller of this store must fail closed.

Connection strategy: one short-lived `psycopg` connection per call, opened through
`connect_from_config` and closed by a `with` block; there is no pool.

`grant` is a plain insert plus one audit event, with no advisory lock, for every grantable
role including `SYSTEM_OWNER`. `revoke` is a plain delete plus one audit event for
`SYSTEM_ADMIN`, `POLICY_MANAGER` and `COMPLIANCE_OFFICER`; revoking `SYSTEM_OWNER` instead
runs an advisory-locked count-and-delete (`_revoke_system_owner`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

import psycopg

import ps_service.authz.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers this component's own audit actions before any grant/revoke/bootstrap call can reach AuditStore.record
from ps_service.authz.errors import (
    AccessRoleAssignmentPersistenceError,
    AccessRoleSystemOwnerFloorRaceError,
)
from ps_service.authz.models import AccessRole, AccessRoleAssignmentRow
from ps_service.persistence import connect_from_config

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from typing import NoReturn

    from ps_service.audit.store import AuditStore
    from ps_service.config import ServiceConfig

# The fixed actor/grantor identity recorded for the bootstrap event and the
# bootstrap `SYSTEM_OWNER`/`AUTHENTICATED_USER` rows: a string that can never
# collide with a real OIDC `sub`, which is always identity-provider issued.
_BOOTSTRAP_SENTINEL = "system:bootstrap"

_BOOTSTRAP_ADVISORY_LOCK_KEY = "ps_authz_bootstrap"
_SYSTEM_OWNER_FLOOR_ADVISORY_LOCK_KEY = "ps_authz_systemowner_floor"
_SYSTEM_OWNER_FLOOR = 1


class AccessRoleStore(Protocol):
    """Persistence seam for `access_role_assignments` rows and their audit events.

    Constructor-injected wherever it is needed. `ps_service.authz.service` depends on this
    `Protocol`, never on `PsycopgAccessRoleStore` directly, so a test can substitute an
    in-memory fake.

    Every method may raise `StatePostgresConnectionError` when the state database is not
    configured or unreachable, and `AccessRoleAssignmentPersistenceError` when a query
    fails; `ps_service.authz.service` translates both into `AuthorizationStoreUnavailableError`.
    """

    def bootstrap_first_owner(self, principal: tuple[str, str]) -> frozenset[AccessRole]:
        """Install `principal` as the first `SystemOwner` if the store is empty and it is the owner.

        Under a database advisory lock, checks whether `access_role_assignments` is empty
        and, if so, whether `principal` equals the configured bootstrap-owner identity.

        Args:
            principal: The caller's verified `(sub, iss)` identity.

        Returns:
            `{AUTHENTICATED_USER, SYSTEM_OWNER}` when this call installed `principal` as the
            first owner. `{AUTHENTICATED_USER}` alone when the store was already non-empty
            once the lock was acquired, or when it was empty but `principal` is not the
            configured bootstrap owner.

        Raises:
            StatePostgresConnectionError: the state database is not configured.
            AccessRoleAssignmentPersistenceError: a database call failed; nothing is
                persisted.

        Side Effects:
            Winning call: inserts one `SYSTEM_OWNER` row and one `AUTHENTICATED_USER` row
            for `principal` and one `access_role.bootstrap` audit event, in one transaction.
            Empty store with a non-matching principal: writes one
            `access_role.bootstrap_rejected` audit event and no assignment row. Store
            already non-empty: writes nothing.
        """
        ...

    def active_roles_for(self, principal: tuple[str, str]) -> frozenset[AccessRole]:
        """Return the access roles persisted for `principal`.

        `AUTHENTICATED_USER` is not implied: it appears only if an explicit row exists, which
        is the case only for the bootstrap principal. The service layer adds it for every
        authenticated caller.

        Args:
            principal: The `(sub, iss)` identity to look up.

        Returns:
            The roles persisted for exactly that `(sub, iss)` pair; empty when it has none.

        Raises:
            StatePostgresConnectionError: the state database is not configured.
            AccessRoleAssignmentPersistenceError: the query failed.

        Side Effects:
            None; read-only.
        """
        ...

    def grant(
        self, *, actor: tuple[str, str], target: tuple[str, str], access_role: AccessRole
    ) -> None:
        """Insert an assignment row for `target` and record an `access_role.grant` audit event.

        The insert is idempotent: an existing row for the same principal and role is left
        unchanged. The audit event is written in the same transaction as the insert.

        Args:
            actor: The granting caller's `(sub, iss)` identity, recorded as the grantor.
            target: The `(sub, iss)` identity receiving the role.
            access_role: The role to grant.

        Raises:
            StatePostgresConnectionError: the state database is not configured.
            AccessRoleAssignmentPersistenceError: a database call failed; nothing is
                persisted.

        Side Effects:
            Inserts one `access_role_assignments` row (unless present) and writes one
            `access_role.grant` audit event with `outcome='applied'`.
        """
        ...

    def revoke(
        self, *, actor: tuple[str, str], target: tuple[str, str], access_role: AccessRole
    ) -> None:
        """Delete the matching assignment row and record an `access_role.revoke` audit event.

        Deleting an absent row is not an error. For `SYSTEM_OWNER`, the count and delete run
        in one transaction under an advisory lock, as the atomic second layer behind the
        pre-mutation floor rule in `ps_service.authz.service.revoke_role`. Other roles use a
        plain, unlocked delete.

        Args:
            actor: The revoking caller's `(sub, iss)` identity.
            target: The `(sub, iss)` identity losing the role.
            access_role: The role to revoke.

        Raises:
            AccessRoleSystemOwnerFloorRaceError: `access_role` is `SYSTEM_OWNER` and the
                locked recount shows at most one active `SystemOwner`; nothing is deleted
                or recorded.
            StatePostgresConnectionError: the state database is not configured.
            AccessRoleAssignmentPersistenceError: a database call failed; nothing is
                persisted.

        Side Effects:
            Deletes at most one `access_role_assignments` row and writes one
            `access_role.revoke` audit event with `outcome='applied'`, in one transaction.
        """
        ...

    def count_active_system_owners(self) -> int:
        """Return how many principals currently hold a `SYSTEM_OWNER` assignment.

        Returns:
            The number of `SYSTEM_OWNER` rows; 0 when there are none.

        Raises:
            StatePostgresConnectionError: the state database is not configured.
            AccessRoleAssignmentPersistenceError: the query failed.

        Side Effects:
            None; read-only.
        """
        ...

    def list_all_assignments(self) -> tuple[AccessRoleAssignmentRow, ...]:
        """Return every row currently in `access_role_assignments`.

        Returns:
            All assignment rows ordered by principal subject, principal issuer and role
            (in the real implementation); empty when the table is empty.

        Raises:
            StatePostgresConnectionError: the state database is not configured.
            AccessRoleAssignmentPersistenceError: the query failed.

        Side Effects:
            None; read-only.
        """
        ...

    def record_grant_rejected(
        self,
        *,
        actor: tuple[str, str],
        target: tuple[str, str],
        access_role: AccessRole,
        reason_code: str,
    ) -> None:
        """Record a denied grant as one `access_role.grant` audit event with `outcome='rejected'`.

        Called by `ps_service.authz.service.grant_role` immediately before it raises a denial
        error. Nothing was mutated, so there is no state-changing transaction to join and the
        event is written on its own connection.

        Args:
            actor: The denied caller's `(sub, iss)` identity.
            target: The `(sub, iss)` identity the denied grant targeted.
            access_role: The role the denied call attempted to grant.
            reason_code: The denial reason, `"access_denied"` or `"self_grant_blocked"`.

        Raises:
            AuditPostgresUnavailableError: the audit database could not be reached.
            AuditPersistenceError: the audit write failed after connecting.
            AuditInvalidDetailsError: `reason_code` is not a value the action's details
                model accepts.
            AuditUnknownActionError: `access_role.grant` is not registered.

        Side Effects:
            Writes one `access_role.grant` audit event with `outcome='rejected'`.
        """
        ...

    def record_revoke_rejected(
        self,
        *,
        actor: tuple[str, str],
        target: tuple[str, str],
        access_role: AccessRole,
        reason_code: str,
    ) -> None:
        """Record a denied revoke as one `access_role.revoke` audit event with `outcome='rejected'`.

        Called by `ps_service.authz.service.revoke_role` immediately before it raises a
        denial error: access denied, self-revoke blocked, or a `SystemOwner` floor violation
        found either by the pre-mutation rule or by the store-level concurrent-revoke race.
        Nothing was mutated, so the event is written on its own connection.

        Args:
            actor: The denied caller's `(sub, iss)` identity.
            target: The `(sub, iss)` identity the denied revoke targeted.
            access_role: The role the denied call attempted to revoke.
            reason_code: The denial reason: `"access_denied"`, `"self_revoke_blocked"` or
                `"system_owner_floor_violation"`.

        Raises:
            AuditPostgresUnavailableError: the audit database could not be reached.
            AuditPersistenceError: the audit write failed after connecting.
            AuditInvalidDetailsError: `reason_code` is not a value the action's details
                model accepts.
            AuditUnknownActionError: `access_role.revoke` is not registered.

        Side Effects:
            Writes one `access_role.revoke` audit event with `outcome='rejected'`.
        """
        ...


__all__ = [
    "AccessRoleStore",
    "PsycopgAccessRoleStore",
]

_INSERT_ASSIGNMENT = """
INSERT INTO access_role_assignments (
    principal_subject, principal_issuer, access_role, granted_by_subject, granted_by_issuer
) VALUES (
    %(principal_subject)s, %(principal_issuer)s, %(access_role)s,
    %(granted_by_subject)s, %(granted_by_issuer)s
)
ON CONFLICT (principal_subject, principal_issuer, access_role) DO NOTHING
"""

_SELECT_ASSIGNMENTS_COLUMNS = (
    "principal_subject, principal_issuer, access_role, granted_at, "
    "granted_by_subject, granted_by_issuer"
)
_SELECT_ACTIVE_ROLES_FOR = (
    f"SELECT {_SELECT_ASSIGNMENTS_COLUMNS} FROM access_role_assignments "  # noqa: S608 - fixed literal, no interpolated user input
    "WHERE principal_subject = %(principal_subject)s AND principal_issuer = %(principal_issuer)s"
)
_SELECT_ALL_ASSIGNMENTS = (
    f"SELECT {_SELECT_ASSIGNMENTS_COLUMNS} FROM access_role_assignments "  # noqa: S608 - fixed literal, no interpolated user input
    "ORDER BY principal_subject, principal_issuer, access_role"
)
_COUNT_ACTIVE_SYSTEM_OWNERS = (
    "SELECT count(*) FROM access_role_assignments WHERE access_role = %(access_role)s"
)
_SELECT_ASSIGNMENTS_EMPTY = "SELECT 1 FROM access_role_assignments LIMIT 1"
_ADVISORY_LOCK = "SELECT pg_advisory_xact_lock(hashtext(%(lock_key)s))"
_DELETE_ASSIGNMENT = (
    "DELETE FROM access_role_assignments WHERE principal_subject = %(principal_subject)s "
    "AND principal_issuer = %(principal_issuer)s AND access_role = %(access_role)s"
)


def _raise_system_owner_floor_race() -> NoReturn:
    """Raise `AccessRoleSystemOwnerFloorRaceError`, kept out of the caller's `try` block."""
    raise AccessRoleSystemOwnerFloorRaceError(
        "revoking this SystemOwner would leave zero active SystemOwners (concurrent-revoke race)"
    )


def _row_from_record(record: Sequence[object]) -> AccessRoleAssignmentRow:
    """Map one raw `psycopg` result row (in `_SELECT_ASSIGNMENTS_COLUMNS` order) to a row object.

    `cast()` is needed at this one boundary because `psycopg`'s tuple rows carry no
    per-column static type; the expected types are fixed by the table schema in
    `migrations/0001_access_role_assignments.sql`.
    """
    (
        principal_subject,
        principal_issuer,
        access_role,
        granted_at,
        granted_by_subject,
        granted_by_issuer,
    ) = record
    return AccessRoleAssignmentRow(
        principal_subject=cast("str", principal_subject),
        principal_issuer=cast("str", principal_issuer),
        access_role=AccessRole(cast("str", access_role)),
        granted_at=cast("datetime", granted_at),
        granted_by_subject=cast("str", granted_by_subject),
        granted_by_issuer=cast("str", granted_by_issuer),
    )


class PsycopgAccessRoleStore:
    """`AccessRoleStore` backed by the PS state PostgreSQL database through `psycopg`.

    Every method opens, uses and closes its own connection (`connect_from_config`); no
    connection is cached across calls. State-changing methods write their audit event
    through the injected `AuditStore` on the same cursor, so the audit row commits or rolls
    back with the state change it documents. Every method raises
    `StatePostgresConnectionError` when `state_postgres_host` is unset.
    """

    def __init__(self, config: ServiceConfig, *, audit_store: AuditStore) -> None:
        """Store the configuration and the audit store; no connection is opened.

        Args:
            config: The service configuration; its `state_postgres_*` fields locate the
                database and `authz_bootstrap_owner_subject`/`authz_bootstrap_owner_issuer`
                name the expected first owner.
            audit_store: The store that records audit events. State-changing methods call
                its `record` with their own cursor so the audit row shares their transaction;
                the `record_*_rejected` methods call `record_standalone`.
        """
        self._config = config
        self._audit_store = audit_store

    def bootstrap_first_owner(self, principal: tuple[str, str]) -> frozenset[AccessRole]:
        """Install `principal` as the first `SystemOwner` if the store is empty and it is the owner.

        Takes a transaction-scoped advisory lock first, so a concurrent caller blocks until
        this transaction ends; its own emptiness check then sees the table non-empty and
        returns the default role set without retrying.

        Args:
            principal: The caller's verified `(sub, iss)` identity.

        Returns:
            `{AUTHENTICATED_USER, SYSTEM_OWNER}` when this call installed `principal` as the
            first owner. `{AUTHENTICATED_USER}` alone when the table was already non-empty, or
            was empty but `principal` is not the configured bootstrap-owner identity.

        Raises:
            StatePostgresConnectionError: `state_postgres_host` is unset.
            AccessRoleAssignmentPersistenceError: a `psycopg` call failed; the transaction is
                not committed.
            AuditUnknownActionError: an audit action is not registered.
            AuditInvalidDetailsError: the audit details do not match the action's model.

        Side Effects:
            Winning call: inserts a `SYSTEM_OWNER` and an `AUTHENTICATED_USER` row for
            `principal`, granted by `"system:bootstrap"`, and writes one
            `access_role.bootstrap` audit event, in one transaction. Empty table with a
            non-matching principal: writes one `access_role.bootstrap_rejected` audit event
            and no assignment row. Non-empty table: writes nothing.
        """
        principal_subject, principal_issuer = principal
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(_ADVISORY_LOCK, {"lock_key": _BOOTSTRAP_ADVISORY_LOCK_KEY})
                cur.execute(_SELECT_ASSIGNMENTS_EMPTY)
                store_is_empty = cur.fetchone() is None
                if not store_is_empty:
                    conn.commit()
                    return frozenset({AccessRole.AUTHENTICATED_USER})
                if principal != (
                    self._config.authz_bootstrap_owner_subject,
                    self._config.authz_bootstrap_owner_issuer,
                ):
                    # The store is empty but this principal is not the
                    # operator-configured first owner: grant nothing, respond
                    # exactly as the non-empty branch above, and record a
                    # distinct 'bootstrap_rejected' audit event naming the
                    # rejected principal.
                    self._audit_store.record(
                        cur,
                        actor_subject=_BOOTSTRAP_SENTINEL,
                        actor_issuer=_BOOTSTRAP_SENTINEL,
                        action="access_role.bootstrap_rejected",
                        resource_type="principal",
                        resource_id=principal_subject,
                        outcome="rejected",
                        details={"reason_code": "bootstrap_identity_mismatch"},
                    )
                    conn.commit()
                    return frozenset({AccessRole.AUTHENTICATED_USER})
                for access_role in (AccessRole.AUTHENTICATED_USER, AccessRole.SYSTEM_OWNER):
                    cur.execute(
                        _INSERT_ASSIGNMENT,
                        {
                            "principal_subject": principal_subject,
                            "principal_issuer": principal_issuer,
                            "access_role": access_role.value,
                            "granted_by_subject": _BOOTSTRAP_SENTINEL,
                            "granted_by_issuer": _BOOTSTRAP_SENTINEL,
                        },
                    )
                self._audit_store.record(
                    cur,
                    actor_subject=_BOOTSTRAP_SENTINEL,
                    actor_issuer=_BOOTSTRAP_SENTINEL,
                    action="access_role.bootstrap",
                    resource_type="principal",
                    resource_id=principal_subject,
                    outcome="applied",
                    details={"access_role": AccessRole.SYSTEM_OWNER.value},
                )
                conn.commit()
        except psycopg.Error as exc:
            raise AccessRoleAssignmentPersistenceError(
                f"failed to bootstrap the first AccessRole owner: {exc}"
            ) from exc
        return frozenset({AccessRole.AUTHENTICATED_USER, AccessRole.SYSTEM_OWNER})

    def active_roles_for(self, principal: tuple[str, str]) -> frozenset[AccessRole]:
        """Return the access roles persisted for `principal`.

        `AUTHENTICATED_USER` is not implied; it appears only if an explicit row exists.

        Args:
            principal: The `(sub, iss)` identity to look up.

        Returns:
            The roles persisted for exactly that pair; empty when it has none.

        Raises:
            StatePostgresConnectionError: `state_postgres_host` is unset.
            AccessRoleAssignmentPersistenceError: the query failed.

        Side Effects:
            None; read-only.
        """
        principal_subject, principal_issuer = principal
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(
                    _SELECT_ACTIVE_ROLES_FOR,
                    {"principal_subject": principal_subject, "principal_issuer": principal_issuer},
                )
                records = cur.fetchall()
        except psycopg.Error as exc:
            raise AccessRoleAssignmentPersistenceError(
                f"failed to look up active AccessRoles for principal: {exc}"
            ) from exc
        return frozenset(_row_from_record(record).access_role for record in records)

    def grant(
        self, *, actor: tuple[str, str], target: tuple[str, str], access_role: AccessRole
    ) -> None:
        """Insert an assignment row for `target` and record an `access_role.grant` audit event.

        A plain, unlocked, idempotent insert used for every grantable role, `SYSTEM_OWNER`
        included; only revoking `SYSTEM_OWNER` needs the advisory lock.

        Args:
            actor: The granting caller's `(sub, iss)` identity, recorded as the grantor.
            target: The `(sub, iss)` identity receiving the role.
            access_role: The role to grant.

        Raises:
            StatePostgresConnectionError: `state_postgres_host` is unset.
            AccessRoleAssignmentPersistenceError: a `psycopg` call failed; nothing is
                committed.
            AuditUnknownActionError: `access_role.grant` is not registered.
            AuditInvalidDetailsError: the audit details do not match the action's model.

        Side Effects:
            Inserts one `access_role_assignments` row (unless one exists) and writes one
            `access_role.grant` audit event with `outcome='applied'`, in one transaction.
        """
        actor_subject, actor_issuer = actor
        target_subject, target_issuer = target
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(
                    _INSERT_ASSIGNMENT,
                    {
                        "principal_subject": target_subject,
                        "principal_issuer": target_issuer,
                        "access_role": access_role.value,
                        "granted_by_subject": actor_subject,
                        "granted_by_issuer": actor_issuer,
                    },
                )
                self._audit_store.record(
                    cur,
                    actor_subject=actor_subject,
                    actor_issuer=actor_issuer,
                    action="access_role.grant",
                    resource_type="principal",
                    resource_id=target_subject,
                    outcome="applied",
                    details={"access_role": access_role.value},
                )
                conn.commit()
        except psycopg.Error as exc:
            raise AccessRoleAssignmentPersistenceError(
                f"failed to grant AccessRole {access_role.value!r}: {exc}"
            ) from exc

    def revoke(
        self, *, actor: tuple[str, str], target: tuple[str, str], access_role: AccessRole
    ) -> None:
        """Delete the matching assignment row and record an `access_role.revoke` audit event.

        `SYSTEM_OWNER` is delegated to an advisory-locked count-and-delete; every other role
        uses a plain, unlocked delete. Deleting an absent row is not an error, and the audit
        event is still written.

        Args:
            actor: The revoking caller's `(sub, iss)` identity.
            target: The `(sub, iss)` identity losing the role.
            access_role: The role to revoke.

        Raises:
            AccessRoleSystemOwnerFloorRaceError: `access_role` is `SYSTEM_OWNER` and the
                locked recount shows at most one active `SystemOwner`; nothing is deleted or
                recorded.
            StatePostgresConnectionError: `state_postgres_host` is unset.
            AccessRoleAssignmentPersistenceError: a `psycopg` call failed; nothing is
                committed.
            AuditUnknownActionError: `access_role.revoke` is not registered.
            AuditInvalidDetailsError: the audit details do not match the action's model.

        Side Effects:
            Deletes at most one `access_role_assignments` row and writes one
            `access_role.revoke` audit event with `outcome='applied'`, in one transaction.
        """
        if access_role is AccessRole.SYSTEM_OWNER:
            self._revoke_system_owner(actor=actor, target=target)
            return
        actor_subject, actor_issuer = actor
        target_subject, target_issuer = target
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(
                    _DELETE_ASSIGNMENT,
                    {
                        "principal_subject": target_subject,
                        "principal_issuer": target_issuer,
                        "access_role": access_role.value,
                    },
                )
                self._audit_store.record(
                    cur,
                    actor_subject=actor_subject,
                    actor_issuer=actor_issuer,
                    action="access_role.revoke",
                    resource_type="principal",
                    resource_id=target_subject,
                    outcome="applied",
                    details={"access_role": access_role.value},
                )
                conn.commit()
        except psycopg.Error as exc:
            raise AccessRoleAssignmentPersistenceError(
                f"failed to revoke AccessRole {access_role.value!r}: {exc}"
            ) from exc

    def _revoke_system_owner(self, *, actor: tuple[str, str], target: tuple[str, str]) -> None:
        """Delete a `SYSTEM_OWNER` assignment under an advisory lock, never emptying the owners.

        Takes a transaction-scoped advisory lock first, which serializes it against any other
        concurrent `SYSTEM_OWNER` revoke, then recounts active `SystemOwner`s. The pre-mutation
        floor rule in `ps_service.authz.service.revoke_role` reads its count without a lock;
        this locked recount is the atomic layer that prevents two concurrent revokes from
        dropping the count to zero.

        Args:
            actor: The revoking caller's `(sub, iss)` identity.
            target: The `(sub, iss)` identity losing `SYSTEM_OWNER`.

        Raises:
            AccessRoleSystemOwnerFloorRaceError: the locked recount shows at most one active
                `SystemOwner`; the transaction is rolled back, so nothing is deleted or
                recorded.
            StatePostgresConnectionError: `state_postgres_host` is unset.
            AccessRoleAssignmentPersistenceError: a `psycopg` call failed; nothing is
                committed.
            AuditUnknownActionError: `access_role.revoke` is not registered.
            AuditInvalidDetailsError: the audit details do not match the action's model.

        Side Effects:
            Deletes at most one `access_role_assignments` row and writes one
            `access_role.revoke` audit event with `outcome='applied'`, in one transaction.
        """
        actor_subject, actor_issuer = actor
        target_subject, target_issuer = target
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(_ADVISORY_LOCK, {"lock_key": _SYSTEM_OWNER_FLOOR_ADVISORY_LOCK_KEY})
                cur.execute(
                    _COUNT_ACTIVE_SYSTEM_OWNERS, {"access_role": AccessRole.SYSTEM_OWNER.value}
                )
                record = cur.fetchone()
                count = cast("int", record[0]) if record is not None else 0
                if count <= _SYSTEM_OWNER_FLOOR:
                    conn.rollback()
                    _raise_system_owner_floor_race()
                cur.execute(
                    _DELETE_ASSIGNMENT,
                    {
                        "principal_subject": target_subject,
                        "principal_issuer": target_issuer,
                        "access_role": AccessRole.SYSTEM_OWNER.value,
                    },
                )
                self._audit_store.record(
                    cur,
                    actor_subject=actor_subject,
                    actor_issuer=actor_issuer,
                    action="access_role.revoke",
                    resource_type="principal",
                    resource_id=target_subject,
                    outcome="applied",
                    details={"access_role": AccessRole.SYSTEM_OWNER.value},
                )
                conn.commit()
        except AccessRoleSystemOwnerFloorRaceError:
            raise
        except psycopg.Error as exc:
            raise AccessRoleAssignmentPersistenceError(
                f"failed to revoke AccessRole {AccessRole.SYSTEM_OWNER.value!r}: {exc}"
            ) from exc

    def count_active_system_owners(self) -> int:
        """Return how many principals currently hold a `SYSTEM_OWNER` assignment.

        Returns:
            The number of `SYSTEM_OWNER` rows; 0 when there are none.

        Raises:
            StatePostgresConnectionError: `state_postgres_host` is unset.
            AccessRoleAssignmentPersistenceError: the query failed or returned no row.

        Side Effects:
            None; read-only.
        """
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(
                    _COUNT_ACTIVE_SYSTEM_OWNERS, {"access_role": AccessRole.SYSTEM_OWNER.value}
                )
                record = cur.fetchone()
        except psycopg.Error as exc:
            raise AccessRoleAssignmentPersistenceError(
                f"failed to count active SystemOwners: {exc}"
            ) from exc
        if record is None:  # pragma: no cover - COUNT(*) always yields exactly one row
            message = "COUNT(*) over access_role_assignments unexpectedly returned no row"
            raise AccessRoleAssignmentPersistenceError(message)
        return cast("int", record[0])

    def list_all_assignments(self) -> tuple[AccessRoleAssignmentRow, ...]:
        """Return every row currently in `access_role_assignments`.

        Returns:
            All assignment rows ordered by principal subject, principal issuer and role;
            empty when the table is empty.

        Raises:
            StatePostgresConnectionError: `state_postgres_host` is unset.
            AccessRoleAssignmentPersistenceError: the query failed.

        Side Effects:
            None; read-only.
        """
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(_SELECT_ALL_ASSIGNMENTS)
                records = cur.fetchall()
        except psycopg.Error as exc:
            raise AccessRoleAssignmentPersistenceError(
                f"failed to list AccessRole assignments: {exc}"
            ) from exc
        return tuple(_row_from_record(record) for record in records)

    def record_grant_rejected(
        self,
        *,
        actor: tuple[str, str],
        target: tuple[str, str],
        access_role: AccessRole,
        reason_code: str,
    ) -> None:
        """Record a denied grant as one `access_role.grant` audit event with `outcome='rejected'`.

        The denial fires before any store mutation, so there is no transaction to join; the
        event is written through `AuditStore.record_standalone`, which opens and commits its
        own connection.

        Args:
            actor: The denied caller's `(sub, iss)` identity.
            target: The `(sub, iss)` identity the denied grant targeted; only its subject is
                recorded as the resource id.
            access_role: The role the denied call attempted to grant.
            reason_code: The denial reason, `"access_denied"` or `"self_grant_blocked"`.

        Raises:
            AuditPostgresUnavailableError: the audit database could not be reached.
            AuditPersistenceError: the audit write failed after connecting.
            AuditInvalidDetailsError: `reason_code` is not accepted by the action's details
                model.
            AuditUnknownActionError: `access_role.grant` is not registered.

        Side Effects:
            Writes one `access_role.grant` audit event with `outcome='rejected'`.
        """
        actor_subject, actor_issuer = actor
        target_subject, _target_issuer = target
        self._audit_store.record_standalone(
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            action="access_role.grant",
            resource_type="principal",
            resource_id=target_subject,
            outcome="rejected",
            details={"access_role": access_role.value, "reason_code": reason_code},
        )

    def record_revoke_rejected(
        self,
        *,
        actor: tuple[str, str],
        target: tuple[str, str],
        access_role: AccessRole,
        reason_code: str,
    ) -> None:
        """Record a denied revoke as one `access_role.revoke` audit event with `outcome='rejected'`.

        The denial fires before any store mutation, so there is no transaction to join; the
        event is written through `AuditStore.record_standalone`, which opens and commits its
        own connection.

        Args:
            actor: The denied caller's `(sub, iss)` identity.
            target: The `(sub, iss)` identity the denied revoke targeted; only its subject is
                recorded as the resource id.
            access_role: The role the denied call attempted to revoke.
            reason_code: The denial reason: `"access_denied"`, `"self_revoke_blocked"` or
                `"system_owner_floor_violation"`.

        Raises:
            AuditPostgresUnavailableError: the audit database could not be reached.
            AuditPersistenceError: the audit write failed after connecting.
            AuditInvalidDetailsError: `reason_code` is not accepted by the action's details
                model.
            AuditUnknownActionError: `access_role.revoke` is not registered.

        Side Effects:
            Writes one `access_role.revoke` audit event with `outcome='rejected'`.
        """
        actor_subject, actor_issuer = actor
        target_subject, _target_issuer = target
        self._audit_store.record_standalone(
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            action="access_role.revoke",
            resource_type="principal",
            resource_id=target_subject,
            outcome="rejected",
            details={"access_role": access_role.value, "reason_code": reason_code},
        )
