"""PostgreSQL persistence for `access_role_assignments`/`access_role_grant_events` (issue #133).

`AccessRoleStore` is the `Protocol` `ps_service.authz.service` depends on --
`PsycopgAccessRoleStore` is the real implementation. Mirrors
`ps_service.passkey_signing.store`'s shape exactly (PLAN.md §0.3/§2.2), with
its own tables, own config surface, own migration-tracking table, and one
deliberate divergence: every method here fails closed (raises
`AccessRolePostgresConnectionError`) when `config.authz_postgres_host` is
unset, rather than no-op'ing -- every `AccessRoleStore` caller has a caller
that must fail closed (PLAN.md §0.11), unlike passkey_signing's own
connectivity probe, which has no such caller.

Connection strategy: one short-lived `psycopg.connect(...)` per call, opened
via `connect_from_config`, closed via `with` -- no pool, mirroring
`ps_service.passkey_signing.store`'s own per-call-connection idiom.

`grant`: a plain insert + one audit event, no advisory lock, for every
grantable role including `SYSTEM_OWNER` (CHANGES.md's MAJOR resolution: only
*revoking* `SYSTEM_OWNER` needs the advisory-locked count-and-delete).
`revoke`: the same plain, unlocked delete + audit event for
`SYSTEM_ADMIN`/`POLICY_MANAGER` (Slice 2); `SYSTEM_OWNER` delegates to
`_revoke_system_owner`'s advisory-locked count-and-delete (Slice 3, §0.10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

import psycopg

from ps_service.authz.errors import (
    AccessRoleAssignmentPersistenceError,
    AccessRolePostgresConnectionError,
    AccessRoleSystemOwnerFloorRaceError,
)
from ps_service.authz.models import AccessRole, AccessRoleAssignmentRow
from ps_service.dependency_health import AUTHZ_POSTGRES, mark_healthy, mark_unhealthy

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from typing import NoReturn

    from psycopg.rows import TupleRow

    from ps_service.config import ServiceConfig

# The fixed actor/grantor identity recorded for the bootstrap event and the
# bootstrap `SYSTEM_OWNER`/`AUTHENTICATED_USER` rows (PLAN.md §1.1) -- a
# string that can never collide with a real OIDC `sub`, which is always
# IdP-issued.
_BOOTSTRAP_SENTINEL = "system:bootstrap"

_BOOTSTRAP_ADVISORY_LOCK_KEY = "ps_authz_bootstrap"
_SYSTEM_OWNER_FLOOR_ADVISORY_LOCK_KEY = "ps_authz_systemowner_floor"
_SYSTEM_OWNER_FLOOR = 1


class AccessRoleStore(Protocol):
    """Persistence seam for `access_role_assignments`/`access_role_grant_events` rows.

    Constructor-injected wherever it is needed (no DI framework, L2's "plain
    constructor injection" rule) -- `ps_service.authz.service` depends on
    this `Protocol`, never on `PsycopgAccessRoleStore` directly, so a test
    can substitute an in-memory fake.
    """

    def bootstrap_first_owner(self, principal: tuple[str, str]) -> frozenset[AccessRole]:
        """Advisory-locked check-empty-then-insert (PLAN.md §0.10).

        Returns the principal's resulting role set:
        `{AUTHENTICATED_USER, SYSTEM_OWNER}` if this call won the bootstrap
        race (the store was empty at lock-acquisition time -- exactly one
        `SYSTEM_OWNER` row and one `AUTHENTICATED_USER` row are persisted for
        `principal`, plus one `'bootstrap'` audit event), else
        `{AUTHENTICATED_USER}` alone (AC-BI-002) if the store was already
        non-empty by the time the lock was acquired -- nothing is written on
        that path.
        """
        ...

    def active_roles_for(self, principal: tuple[str, str]) -> frozenset[AccessRole]:
        """Current active `AccessRole` set for `principal` (no `AUTHENTICATED_USER` implied here).

        Callers combine this with bootstrap-or-default logic in `service.py`
        -- AC-BI-002's "default to `AuthenticatedUser` only" is a
        service-layer fact about every already-authenticated caller, not a
        persisted row (the one exception being the bootstrap principal's own
        explicit `AUTHENTICATED_USER` row, PLAN.md §1.1).
        """
        ...

    def grant(
        self, *, actor: tuple[str, str], target: tuple[str, str], access_role: AccessRole
    ) -> None:
        """Idempotent insert into `access_role_assignments` + one `'grant'` audit event."""
        ...

    def revoke(
        self, *, actor: tuple[str, str], target: tuple[str, str], access_role: AccessRole
    ) -> None:
        """`DELETE` the matching assignment row (no-op if absent) + one `'revoke'` audit event.

        For `access_role == SYSTEM_OWNER`, wraps the count-check + delete in
        the advisory-locked transaction from PLAN.md §0.10 (defense-in-depth;
        `ps_service.authz.service.revoke_role`'s own `rules.
        enforce_system_owner_floor` check is the primary, pre-mutation guard)
        -- raises `AccessRoleSystemOwnerFloorRaceError` instead of deleting if
        the advisory-locked recount still shows exactly one active
        `SystemOwner` (PLAN.md §4 Slice 3). `SYSTEM_ADMIN`/`POLICY_MANAGER`
        use the plain, unlocked path.
        """
        ...

    def count_active_system_owners(self) -> int:
        """Return how many principals currently hold an active `SYSTEM_OWNER` assignment."""
        ...

    def list_all_assignments(self) -> tuple[AccessRoleAssignmentRow, ...]:
        """Return every row currently in `access_role_assignments`."""
        ...


__all__ = [
    "AccessRoleStore",
    "PsycopgAccessRoleStore",
    "check_connectivity_from_config",
    "connect_from_config",
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

_INSERT_GRANT_EVENT = """
INSERT INTO access_role_grant_events (
    event_type, actor_subject, actor_issuer, target_subject, target_issuer, access_role
) VALUES (
    %(event_type)s, %(actor_subject)s, %(actor_issuer)s, %(target_subject)s, %(target_issuer)s,
    %(access_role)s
)
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
    """Raise `AccessRoleSystemOwnerFloorRaceError`.

    Abstracted into its own function per ruff's TRY301: a `raise` inside a
    `try` block, where the same block's own `except` clause could otherwise
    appear to catch it, belongs in its own function, not inline.
    """
    raise AccessRoleSystemOwnerFloorRaceError(
        "revoking this SystemOwner would leave zero active SystemOwners (concurrent-revoke race)"
    )


def connect_from_config(config: ServiceConfig) -> psycopg.Connection[TupleRow]:
    """Open a fresh `psycopg` connection from `config.authz_postgres_*`.

    Mirrors `ps_service.passkey_signing.store.connect_from_config`'s
    per-call-connection idiom, with one deliberate divergence (PLAN.md
    §0.11): raises `AccessRolePostgresConnectionError` immediately when
    `config.authz_postgres_host` is `None`, without attempting a doomed
    `psycopg.connect(host=None, ...)` call -- every `AccessRoleStore` caller
    must fail closed rather than silently no-op.
    """
    if config.authz_postgres_host is None:
        raise AccessRolePostgresConnectionError(
            "Authz Postgres is not configured (PS_AUTHZ_POSTGRES_HOST is unset); "
            "every role-gated action fails closed until it is configured."
        )
    return psycopg.connect(
        host=config.authz_postgres_host,
        port=config.authz_postgres_port,
        dbname=config.authz_postgres_database,
        user=config.authz_postgres_user,
        password=config.authz_postgres_password,
    )


def check_connectivity_from_config(config: ServiceConfig) -> None:
    """Probe the Authz Postgres instance.

    Unlike `ps_service.passkey_signing.store.check_connectivity_from_config`
    (a no-op when unconfigured), an unconfigured Authz Postgres is treated
    as unreachable too (PLAN.md §0.11/§2.2): every role-gated action must
    fail closed when this store cannot be reached, so "unconfigured" is not
    a healthy "not applicable" state here -- it is the store being down.

    Raises:
        AccessRolePostgresConnectionError: unconfigured, or configured but
            unreachable (connection failure or the round-trip query itself
            fails); the outcome is also recorded in
            `ps_service.dependency_health`.
    """
    try:
        with connect_from_config(config) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except AccessRolePostgresConnectionError as exc:
        mark_unhealthy(AUTHZ_POSTGRES, error=exc)
        raise
    except psycopg.Error as exc:
        mark_unhealthy(AUTHZ_POSTGRES, error=exc)
        raise AccessRolePostgresConnectionError(
            "Authz Postgres connection failed at "
            f"{config.authz_postgres_host}:{config.authz_postgres_port}. "
            f"Is Postgres running? Error: {exc}"
        ) from exc
    mark_healthy(AUTHZ_POSTGRES)


def _row_from_record(record: Sequence[object]) -> AccessRoleAssignmentRow:
    """Map one raw `psycopg` result row (fixed column order, see `_SELECT_ASSIGNMENTS_COLUMNS`).

    `cast()` is unavoidable at exactly this one boundary (L2's `cast()`
    policy): `psycopg`'s tuple-row result carries no per-column static type
    without a per-query `Row` type parameter, which is not worth introducing
    for these few call sites. Every column's expected Python type is fixed
    by `migrations/0001_access_role_assignments.sql`'s own schema.
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
    """Real `AccessRoleStore` backed by PostgreSQL via `psycopg[binary]` (PLAN.md §2.2).

    Every method opens, uses, and closes its own connection (`with
    connect_from_config(self._config) as conn`) -- no pool, no cached
    connection held across calls.
    """

    def __init__(self, config: ServiceConfig) -> None:
        """Store `config`; no connection is opened until a method is called."""
        self._config = config

    def bootstrap_first_owner(self, principal: tuple[str, str]) -> frozenset[AccessRole]:
        """Advisory-locked check-empty-then-insert (PLAN.md §0.10, AC-BI-001/002).

        Acquires `pg_advisory_xact_lock(hashtext('ps_authz_bootstrap'))` as
        the first statement inside its transaction, before checking whether
        `access_role_assignments` is empty. Postgres serializes any second
        concurrent caller's advisory-lock call behind the first
        transaction's commit/rollback (the lock auto-releases at transaction
        end) -- the loser's own emptiness check, run after acquiring the
        lock, sees the table non-empty and falls through to AC-BI-002's
        default path within the very same call, no retry loop needed.
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
                    # AC-BI-004/AC-BI-005: the store is empty, but this
                    # principal is not the operator-configured expected first
                    # owner -- grant nothing, leave the table empty, respond
                    # exactly as the non-empty-store branch above (D-4), and
                    # record a distinct 'bootstrap_rejected' audit event
                    # naming the rejected principal (D-5), mirroring
                    # `FakeAccessRoleStore.bootstrap_first_owner`'s own
                    # no-match branch (`tests/authz/_fakes.py`) exactly.
                    cur.execute(
                        _INSERT_GRANT_EVENT,
                        {
                            "event_type": "bootstrap_rejected",
                            "actor_subject": _BOOTSTRAP_SENTINEL,
                            "actor_issuer": _BOOTSTRAP_SENTINEL,
                            "target_subject": principal_subject,
                            "target_issuer": principal_issuer,
                            "access_role": AccessRole.SYSTEM_OWNER.value,
                        },
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
                cur.execute(
                    _INSERT_GRANT_EVENT,
                    {
                        "event_type": "bootstrap",
                        "actor_subject": _BOOTSTRAP_SENTINEL,
                        "actor_issuer": _BOOTSTRAP_SENTINEL,
                        "target_subject": principal_subject,
                        "target_issuer": principal_issuer,
                        "access_role": AccessRole.SYSTEM_OWNER.value,
                    },
                )
                conn.commit()
        except psycopg.Error as exc:
            raise AccessRoleAssignmentPersistenceError(
                f"failed to bootstrap the first AccessRole owner: {exc}"
            ) from exc
        return frozenset({AccessRole.AUTHENTICATED_USER, AccessRole.SYSTEM_OWNER})

    def active_roles_for(self, principal: tuple[str, str]) -> frozenset[AccessRole]:
        """Current active `AccessRole` set for `principal` (no `AUTHENTICATED_USER` implied)."""
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
        """Idempotent insert into `access_role_assignments` + one `'grant'` audit event.

        Plain, unlocked insert/insert -- used for every grantable role,
        `SYSTEM_OWNER` included (CHANGES.md MAJOR resolution: only
        *revoking* `SYSTEM_OWNER` needs the advisory lock, PLAN.md §0.10,
        which is Slice 3's own scope).
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
                cur.execute(
                    _INSERT_GRANT_EVENT,
                    {
                        "event_type": "grant",
                        "actor_subject": actor_subject,
                        "actor_issuer": actor_issuer,
                        "target_subject": target_subject,
                        "target_issuer": target_issuer,
                        "access_role": access_role.value,
                    },
                )
                conn.commit()
        except psycopg.Error as exc:
            raise AccessRoleAssignmentPersistenceError(
                f"failed to grant AccessRole {access_role.value!r}: {exc}"
            ) from exc

    def revoke(
        self, *, actor: tuple[str, str], target: tuple[str, str], access_role: AccessRole
    ) -> None:
        """`DELETE` the matching assignment row (no-op if absent) + one `'revoke'` audit event.

        `SYSTEM_OWNER` is delegated to `_revoke_system_owner`'s
        advisory-locked count-and-delete (PLAN.md §0.10/§4 Slice 3);
        `SYSTEM_ADMIN`/`POLICY_MANAGER` use this plain, unlocked path.
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
                cur.execute(
                    _INSERT_GRANT_EVENT,
                    {
                        "event_type": "revoke",
                        "actor_subject": actor_subject,
                        "actor_issuer": actor_issuer,
                        "target_subject": target_subject,
                        "target_issuer": target_issuer,
                        "access_role": access_role.value,
                    },
                )
                conn.commit()
        except psycopg.Error as exc:
            raise AccessRoleAssignmentPersistenceError(
                f"failed to revoke AccessRole {access_role.value!r}: {exc}"
            ) from exc

    def _revoke_system_owner(self, *, actor: tuple[str, str], target: tuple[str, str]) -> None:
        """Advisory-locked count-and-delete for revoking `SYSTEM_OWNER` (PLAN.md §0.10).

        Acquires `pg_advisory_xact_lock(hashtext('ps_authz_systemowner_floor'))`
        as the first statement inside its transaction, before recounting
        active `SystemOwner`s -- serializes this method against any other
        concurrent `SYSTEM_OWNER` revoke, exactly mirroring
        `bootstrap_first_owner`'s own advisory-lock idiom with a second,
        distinct lock key. `ps_service.authz.service.revoke_role`'s own
        `rules.enforce_system_owner_floor` check (evaluated against a count
        read *before* this call, with no lock held) is the primary guard;
        this recount, taken under the lock immediately before the delete, is
        the second, atomic layer that actually prevents a genuine
        concurrent-revoke race from dropping the count to zero.

        Raises:
            AccessRoleSystemOwnerFloorRaceError: the locked recount still
                shows exactly one active `SystemOwner` -- deleting it would
                leave zero, so nothing is deleted or recorded.
            AccessRoleAssignmentPersistenceError: the underlying `psycopg`
                call failed.
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
                cur.execute(
                    _INSERT_GRANT_EVENT,
                    {
                        "event_type": "revoke",
                        "actor_subject": actor_subject,
                        "actor_issuer": actor_issuer,
                        "target_subject": target_subject,
                        "target_issuer": target_issuer,
                        "access_role": AccessRole.SYSTEM_OWNER.value,
                    },
                )
                conn.commit()
        except AccessRoleSystemOwnerFloorRaceError:
            raise
        except psycopg.Error as exc:
            raise AccessRoleAssignmentPersistenceError(
                f"failed to revoke AccessRole {AccessRole.SYSTEM_OWNER.value!r}: {exc}"
            ) from exc

    def count_active_system_owners(self) -> int:
        """Return how many principals currently hold an active `SYSTEM_OWNER` assignment."""
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
        """Return every row currently in `access_role_assignments`."""
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(_SELECT_ALL_ASSIGNMENTS)
                records = cur.fetchall()
        except psycopg.Error as exc:
            raise AccessRoleAssignmentPersistenceError(
                f"failed to list AccessRole assignments: {exc}"
            ) from exc
        return tuple(_row_from_record(record) for record in records)
