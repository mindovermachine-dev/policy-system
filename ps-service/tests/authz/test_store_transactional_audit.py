"""Tests proving `PsycopgAccessRoleStore` writes through `AuditStore` in-transaction (issue #147).

`postgres_live`-marked throughout: whether `grant`/`revoke`/`bootstrap_first_owner`
actually persist exactly one `audit_events` row alongside their own state
change, in the *same* Postgres transaction (so a failure in either half rolls
back both), can only be proven against a real Postgres instance.

PLAN.md/CHANGES.md Appendix A1's Slice 2 boundary: this is the slice that
repoints `ps_service.authz.store` off the retired `access_role_grant_events`
table (absent from the baseline schema, `tests/persistence/test_migration_runner.py`) and onto
`AuditStore.record`, in the same cursor/transaction as the state-changing
`INSERT`/`DELETE` -- AC-BI-006 ("exactly one `audit_events` row... and no
code path writes to `access_role_grant_events`") and AC-BI-010 (an
`AuditStore.record` failure rolls back the state change too) are this file's
whole job.

Slice 3 (below the Slice 2 tests) adds AC-BI-012's own real-Postgres proof:
each of the four denial types (access denied, self-grant/revoke, SystemOwner
floor, bootstrap identity mismatch) writes exactly one `outcome='rejected'`
row, reached via the real `ps_service.authz.service.grant_role`/`revoke_role`/
`PsycopgAccessRoleStore.bootstrap_first_owner` call chain -- not a synthetic
call directly against `AuditStore`. Plus CHANGES.md item 3's leak-check: a
`record_standalone` connection failure never leaks host/port/driver detail.

Deselected by default (BASELINE.md's tier gating) -- run explicitly with
`uv run pytest -m postgres_live` against a reachable `PS_STATE_POSTGRES_*`
instance.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import TYPE_CHECKING, cast

import psycopg
import pytest

from ps_service.api.errors import (
    AccessDeniedError,
    SelfGrantOrRevokeBlockedError,
    SystemOwnerFloorViolationError,
)
from ps_service.audit import MIGRATIONS_DIR as AUDIT_MIGRATIONS_DIR
from ps_service.audit.errors import AuditPostgresUnavailableError
from ps_service.audit.store import PsycopgAuditStore
from ps_service.authz import MIGRATIONS_DIR as AUTHZ_MIGRATIONS_DIR
from ps_service.authz.errors import AccessRoleAssignmentPersistenceError
from ps_service.authz.models import AccessRole
from ps_service.authz.service import grant_role, revoke_role
from ps_service.authz.store import PsycopgAccessRoleStore
from ps_service.config import load_config
from ps_service.persistence import MigrationSource, apply_pending_migrations, connect_from_config

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Literal, LiteralString

    from psycopg.rows import TupleRow

    from ps_service.audit.models import AuditQueryFilters, AuditQueryPage


# Mirrors the source list `ps_service.main` passes to the runner at startup.
STATE_MIGRATION_SOURCES = [
    MigrationSource("audit", AUDIT_MIGRATIONS_DIR),
    MigrationSource("authz", AUTHZ_MIGRATIONS_DIR),
]

_ACTOR_ISSUER = "https://issuer.example.com/"


def _require_configured_postgres() -> None:
    config = load_config()
    assert config.state_postgres_host is not None, (
        "postgres_live requires PS_STATE_POSTGRES_HOST to be set"
    )


def _unique_subject(label: str) -> str:
    """A collision-free subject for this test run -- the shared `public` schema
    is reused across the whole `postgres_live` session (see
    `test_migration_runner.py`'s own docstring for why), so every row this
    file writes must be uniquely identifiable to avoid clashing with rows any
    other test leaves behind.
    """
    return f"{label}-{uuid.uuid4().hex[:12]}"


class _RaisingAuditStore:
    """An `AuditStore`-shaped fake whose `record` always raises `psycopg.Error`.

    Used to simulate the `audit_events` insert itself failing (AC-BI-010),
    without needing to actually break the real Postgres schema (which would
    risk affecting other tests sharing this session's database). Injected in
    place of the real `PsycopgAuditStore` into `PsycopgAccessRoleStore`, so
    the *state* change (`access_role_assignments`) is still attempted
    against real Postgres, inside the *same* transaction -- proving the
    real `with connect_from_config(...) as conn:` rollback-on-exception
    behavior genuinely undoes the state INSERT/DELETE too, not just the
    audit insert.
    """

    def record(
        self,
        cur: psycopg.Cursor[TupleRow],
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: Literal["applied", "rejected", "failed"],
        details: Mapping[str, object],
    ) -> str:
        del cur, actor_subject, actor_issuer, action, resource_type, resource_id, outcome, details
        raise psycopg.errors.OperationalError("simulated audit_events insert failure")

    def record_standalone(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: Literal["applied", "rejected", "failed"],
        details: Mapping[str, object],
    ) -> None:
        """Not exercised by this fake's own tests -- present only for `Protocol` conformance."""
        del actor_subject, actor_issuer, action, resource_type, resource_id, outcome, details
        raise psycopg.errors.OperationalError("simulated audit_events insert failure")

    def query(
        self, *, filters: AuditQueryFilters, cursor: str | None, page_size: int
    ) -> AuditQueryPage:
        """Not exercised by this fake's own tests -- present only for `Protocol` conformance."""
        del filters, cursor, page_size
        raise psycopg.errors.OperationalError("simulated audit_events insert failure")


def _select_audit_events_for(
    conn: psycopg.Connection[TupleRow], *, resource_id: str, action: str
) -> list[tuple[object, ...]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT actor_subject, actor_issuer, outcome, details FROM audit_events "
            "WHERE resource_id = %(resource_id)s AND action = %(action)s",
            {"resource_id": resource_id, "action": action},
        )
        return cur.fetchall()


def _active_roles_for(conn: psycopg.Connection[TupleRow], *, subject: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT access_role FROM access_role_assignments WHERE principal_subject = %(subject)s",
            {"subject": subject},
        )
        return {record[0] for record in cur.fetchall()}


@pytest.mark.postgres_live
def test_no_code_path_writes_to_the_dropped_access_role_grant_events_table() -> None:
    """AC-BI-006's second half: the retired grant-events table is absent."""
    _require_configured_postgres()
    config = load_config()

    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = "
                "'access_role_grant_events'"
            )
            table_exists = cur.fetchone() is not None

    assert table_exists is False


@pytest.mark.postgres_live
def test_grant_produces_exactly_one_applied_audit_event_alongside_the_assignment_row() -> None:
    """AC-BI-006: a real `grant()` call writes exactly one `access_role_assignments` row
    and exactly one `audit_events` row (`action='access_role.grant'`, `outcome='applied'`).
    """
    _require_configured_postgres()
    config = load_config()
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)

    actor_subject = _unique_subject("grant-actor")
    target_subject = _unique_subject("grant-target")
    store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))

    store.grant(
        actor=(actor_subject, _ACTOR_ISSUER),
        target=(target_subject, _ACTOR_ISSUER),
        access_role=AccessRole.SYSTEM_ADMIN,
    )

    with connect_from_config(config) as conn:
        roles = _active_roles_for(conn, subject=target_subject)
        events = _select_audit_events_for(
            conn, resource_id=target_subject, action="access_role.grant"
        )

    assert roles == {"SystemAdmin"}
    assert events == [(actor_subject, _ACTOR_ISSUER, "applied", {"access_role": "SystemAdmin"})]


@pytest.mark.postgres_live
def test_revoke_produces_exactly_one_applied_audit_event_and_removes_the_assignment() -> None:
    """AC-BI-006: a real `revoke()` call (non-`SystemOwner` path) deletes the assignment
    row and writes exactly one `audit_events` row (`action='access_role.revoke'`,
    `outcome='applied'`).
    """
    _require_configured_postgres()
    config = load_config()
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)

    actor_subject = _unique_subject("revoke-actor")
    target_subject = _unique_subject("revoke-target")
    store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
    store.grant(
        actor=(actor_subject, _ACTOR_ISSUER),
        target=(target_subject, _ACTOR_ISSUER),
        access_role=AccessRole.POLICY_MANAGER,
    )

    store.revoke(
        actor=(actor_subject, _ACTOR_ISSUER),
        target=(target_subject, _ACTOR_ISSUER),
        access_role=AccessRole.POLICY_MANAGER,
    )

    with connect_from_config(config) as conn:
        roles = _active_roles_for(conn, subject=target_subject)
        events = _select_audit_events_for(
            conn, resource_id=target_subject, action="access_role.revoke"
        )

    assert roles == set()
    assert events == [(actor_subject, _ACTOR_ISSUER, "applied", {"access_role": "PolicyManager"})]


@pytest.mark.postgres_live
def test_revoke_of_system_owner_produces_exactly_one_applied_audit_event() -> None:
    """AC-BI-006, `_revoke_system_owner`'s own advisory-locked path: revoking `SystemOwner`
    (with a second owner already present, so the floor check passes) writes exactly one
    `audit_events` row through the same mechanism as the plain revoke path.
    """
    _require_configured_postgres()
    config = load_config()
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)

    actor_subject = _unique_subject("owner-revoke-actor")
    first_owner = _unique_subject("owner-a")
    second_owner = _unique_subject("owner-b")
    store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
    store.grant(
        actor=(actor_subject, _ACTOR_ISSUER),
        target=(first_owner, _ACTOR_ISSUER),
        access_role=AccessRole.SYSTEM_OWNER,
    )
    store.grant(
        actor=(actor_subject, _ACTOR_ISSUER),
        target=(second_owner, _ACTOR_ISSUER),
        access_role=AccessRole.SYSTEM_OWNER,
    )

    store.revoke(
        actor=(actor_subject, _ACTOR_ISSUER),
        target=(first_owner, _ACTOR_ISSUER),
        access_role=AccessRole.SYSTEM_OWNER,
    )

    with connect_from_config(config) as conn:
        roles = _active_roles_for(conn, subject=first_owner)
        events = _select_audit_events_for(
            conn, resource_id=first_owner, action="access_role.revoke"
        )

    assert roles == set()
    assert events == [(actor_subject, _ACTOR_ISSUER, "applied", {"access_role": "SystemOwner"})]


@pytest.mark.postgres_live
def test_bootstrap_first_owner_produces_exactly_one_applied_bootstrap_audit_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-006: the winning `bootstrap_first_owner` call writes exactly one
    `audit_events` row (`action='access_role.bootstrap'`, `outcome='applied'`) alongside
    its two `access_role_assignments` rows (`AuthenticatedUser` + `SystemOwner`).

    `bootstrap_first_owner` only takes the "winning" branch against a
    genuinely *empty* `access_role_assignments` table -- the shared `public`
    schema this whole `postgres_live` session reuses is never guaranteed
    empty (other tests, in this file and others, populate it). This test
    monkeypatches `ps_service.authz.store.connect_from_config` (the module
    function `PsycopgAccessRoleStore`'s every method calls to open its own
    connection) so every connection it opens -- including the one
    `bootstrap_first_owner` itself opens internally -- is pinned via
    `search_path` to a brand-new, empty, throwaway schema instead. This is
    still the real `PsycopgAccessRoleStore`/`PsycopgAuditStore` code path
    against real Postgres; only *which schema* it targets is redirected.
    """
    _require_configured_postgres()
    owner_subject = _unique_subject("bootstrap-owner")
    owner_issuer = _ACTOR_ISSUER
    monkeypatch.setenv("PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT", owner_subject)
    monkeypatch.setenv("PS_AUTHZ_BOOTSTRAP_OWNER_ISSUER", owner_issuer)
    config = load_config()
    schema = f"authz_test_{uuid.uuid4().hex}"

    setup_conn = connect_from_config(config)
    with setup_conn.cursor() as cur:
        cur.execute(cast("LiteralString", f'CREATE SCHEMA "{schema}"'))
        cur.execute(cast("LiteralString", f'SET search_path TO "{schema}"'))
    setup_conn.commit()
    apply_pending_migrations(setup_conn, sources=STATE_MIGRATION_SOURCES)

    def _isolated_connect_from_config(cfg: object) -> psycopg.Connection[TupleRow]:
        del cfg
        conn = connect_from_config(config)
        with conn.cursor() as cur:
            cur.execute(cast("LiteralString", f'SET search_path TO "{schema}"'))
        return conn

    monkeypatch.setattr("ps_service.authz.store.connect_from_config", _isolated_connect_from_config)

    store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
    result = store.bootstrap_first_owner((owner_subject, owner_issuer))

    assert result == frozenset({AccessRole.AUTHENTICATED_USER, AccessRole.SYSTEM_OWNER})

    events = _select_audit_events_for(
        setup_conn, resource_id=owner_subject, action="access_role.bootstrap"
    )
    roles = _active_roles_for(setup_conn, subject=owner_subject)

    with setup_conn.cursor() as cur:
        cur.execute(cast("LiteralString", f'DROP SCHEMA "{schema}" CASCADE'))
    setup_conn.commit()
    setup_conn.close()

    assert roles == {"AuthenticatedUser", "SystemOwner"}
    assert events == [
        ("system:bootstrap", "system:bootstrap", "applied", {"access_role": "SystemOwner"})
    ]


@pytest.mark.postgres_live
def test_audit_store_record_failure_during_grant_rolls_back_the_assignment_insert() -> None:
    """AC-BI-010: when `AuditStore.record` fails, `grant`'s own `access_role_assignments`
    `INSERT` is not applied either (same transaction rolls back both), and the caller
    gets a named `error:`-shaped exception (`AccessRoleAssignmentPersistenceError`).
    """
    _require_configured_postgres()
    config = load_config()
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)

    actor_subject = _unique_subject("failing-grant-actor")
    target_subject = _unique_subject("failing-grant-target")
    store = PsycopgAccessRoleStore(config, audit_store=_RaisingAuditStore())

    with pytest.raises(AccessRoleAssignmentPersistenceError):
        store.grant(
            actor=(actor_subject, _ACTOR_ISSUER),
            target=(target_subject, _ACTOR_ISSUER),
            access_role=AccessRole.SYSTEM_ADMIN,
        )

    with connect_from_config(config) as conn:
        roles = _active_roles_for(conn, subject=target_subject)
        events = _select_audit_events_for(
            conn, resource_id=target_subject, action="access_role.grant"
        )

    assert roles == set()  # the INSERT was rolled back, not just the audit insert
    assert events == []


@pytest.mark.postgres_live
def test_audit_store_record_failure_during_revoke_rolls_back_the_assignment_delete() -> None:
    """AC-BI-010, revoke side: when `AuditStore.record` fails during `revoke`, the
    `access_role_assignments` `DELETE` is not applied either -- the target keeps the
    role it had before the failed revoke attempt.
    """
    _require_configured_postgres()
    config = load_config()
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)

    actor_subject = _unique_subject("failing-revoke-actor")
    target_subject = _unique_subject("failing-revoke-target")
    working_store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
    working_store.grant(
        actor=(actor_subject, _ACTOR_ISSUER),
        target=(target_subject, _ACTOR_ISSUER),
        access_role=AccessRole.POLICY_MANAGER,
    )

    failing_store = PsycopgAccessRoleStore(config, audit_store=_RaisingAuditStore())
    with pytest.raises(AccessRoleAssignmentPersistenceError):
        failing_store.revoke(
            actor=(actor_subject, _ACTOR_ISSUER),
            target=(target_subject, _ACTOR_ISSUER),
            access_role=AccessRole.POLICY_MANAGER,
        )

    with connect_from_config(config) as conn:
        roles = _active_roles_for(conn, subject=target_subject)
        # Only the one 'applied' grant event from the working store above --
        # the failed revoke's own audit insert (and the DELETE it would have
        # accompanied) never landed.
        events = _select_audit_events_for(
            conn, resource_id=target_subject, action="access_role.revoke"
        )

    assert roles == {"PolicyManager"}  # the DELETE was rolled back too
    assert events == []


# --- Slice 3: AC-BI-012 denial-recording, real Postgres proof ------------------------------


@pytest.mark.postgres_live
def test_record_grant_rejected_writes_exactly_one_rejected_audit_event() -> None:
    """`PsycopgAccessRoleStore.record_grant_rejected` writes exactly one `outcome='rejected'`
    `access_role.grant` audit event, via `AuditStore.record_standalone`'s own connection
    (no surrounding state-changing transaction exists for a denial to join).
    """
    _require_configured_postgres()
    config = load_config()
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)

    actor_subject = _unique_subject("rejected-grant-actor")
    target_subject = _unique_subject("rejected-grant-target")
    store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))

    store.record_grant_rejected(
        actor=(actor_subject, _ACTOR_ISSUER),
        target=(target_subject, _ACTOR_ISSUER),
        access_role=AccessRole.SYSTEM_ADMIN,
        reason_code="access_denied",
    )

    with connect_from_config(config) as conn:
        roles = _active_roles_for(conn, subject=target_subject)
        events = _select_audit_events_for(
            conn, resource_id=target_subject, action="access_role.grant"
        )

    assert roles == set()  # a denial never mutates access_role_assignments
    assert events == [
        (
            actor_subject,
            _ACTOR_ISSUER,
            "rejected",
            {"access_role": "SystemAdmin", "reason_code": "access_denied"},
        )
    ]


@pytest.mark.postgres_live
def test_record_revoke_rejected_writes_exactly_one_rejected_audit_event() -> None:
    """`PsycopgAccessRoleStore.record_revoke_rejected` writes exactly one `outcome='rejected'`
    `access_role.revoke` audit event, same mechanism as `record_grant_rejected`.
    """
    _require_configured_postgres()
    config = load_config()
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)

    actor_subject = _unique_subject("rejected-revoke-actor")
    target_subject = _unique_subject("rejected-revoke-target")
    store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))

    store.record_revoke_rejected(
        actor=(actor_subject, _ACTOR_ISSUER),
        target=(target_subject, _ACTOR_ISSUER),
        access_role=AccessRole.SYSTEM_OWNER,
        reason_code="system_owner_floor_violation",
    )

    with connect_from_config(config) as conn:
        events = _select_audit_events_for(
            conn, resource_id=target_subject, action="access_role.revoke"
        )

    assert events == [
        (
            actor_subject,
            _ACTOR_ISSUER,
            "rejected",
            {"access_role": "SystemOwner", "reason_code": "system_owner_floor_violation"},
        )
    ]


@pytest.mark.postgres_live
def test_denied_grant_produces_exactly_one_rejected_audit_event() -> None:
    """AC-BI-012, access-denied denial: a real `grant_role` call by an actor holding no
    grant-eligible role writes exactly one rejected `access_role.grant` audit event -- the
    real service-layer call chain, not a synthetic call directly against a store method.
    """
    _require_configured_postgres()
    config = load_config()
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)

    actor_subject = _unique_subject("denied-grant-actor")
    target_subject = _unique_subject("denied-grant-target")
    store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))

    with pytest.raises(AccessDeniedError):
        grant_role(
            actor=(actor_subject, _ACTOR_ISSUER),
            target_subject=target_subject,
            access_role="SystemAdmin",
            store=store,
            issuer=_ACTOR_ISSUER,
        )

    with connect_from_config(config) as conn:
        events = _select_audit_events_for(
            conn, resource_id=target_subject, action="access_role.grant"
        )

    assert events == [
        (
            actor_subject,
            _ACTOR_ISSUER,
            "rejected",
            {"access_role": "SystemAdmin", "reason_code": "access_denied"},
        )
    ]


@pytest.mark.postgres_live
def test_denied_self_revoke_produces_exactly_one_rejected_audit_event() -> None:
    """AC-BI-012, self-revoke-blocked denial: a real `revoke_role` call where the actor
    targets their own subject writes exactly one rejected `access_role.revoke` audit event.

    Post-#182 `block_self_target` exempts a `SystemOwner` from the self-grant/revoke block
    except for revoking `SystemOwner` itself, and a non-`SystemOwner` is turned away by RBAC
    before the self-target rule is reached -- so the only reachable self-revoke denial is a
    `SystemOwner` revoking their own `SystemOwner` role.
    """
    _require_configured_postgres()
    config = load_config()
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)

    actor_subject = _unique_subject("self-revoke-actor")
    store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
    # Setup (not under test): give the actor SystemOwner directly. RBAC lets them revoke
    # SystemOwner, and block_self_target must still fire before the floor check.
    store.grant(
        actor=(actor_subject, _ACTOR_ISSUER),
        target=(actor_subject, _ACTOR_ISSUER),
        access_role=AccessRole.SYSTEM_OWNER,
    )

    with pytest.raises(SelfGrantOrRevokeBlockedError):
        revoke_role(
            actor=(actor_subject, _ACTOR_ISSUER),
            target_subject=actor_subject,
            access_role="SystemOwner",
            store=store,
            issuer=_ACTOR_ISSUER,
        )

    with connect_from_config(config) as conn:
        events = _select_audit_events_for(
            conn, resource_id=actor_subject, action="access_role.revoke"
        )

    assert events == [
        (
            actor_subject,
            _ACTOR_ISSUER,
            "rejected",
            {"access_role": "SystemOwner", "reason_code": "self_revoke_blocked"},
        )
    ]


@pytest.mark.postgres_live
def test_denied_system_owner_floor_revoke_produces_exactly_one_rejected_audit_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-012, SystemOwner-floor denial: a real `revoke_role` call against the sole
    remaining active SystemOwner writes exactly one rejected `access_role.revoke` audit event.

    Uses a throwaway, isolated schema (same technique as
    `test_bootstrap_first_owner_produces_exactly_one_applied_bootstrap_audit_event`):
    `count_active_system_owners()` counts every SystemOwner in the whole shared `public`
    schema this `postgres_live` session reuses, and other tests in this file deliberately
    leave a SystemOwner behind -- "exactly one active SystemOwner" cannot be guaranteed
    there. Both `ps_service.authz.store.connect_from_config` (the state-change/RBAC-read
    connections) and `ps_service.audit.store.connect_from_config` (the denial-recording
    `record_standalone` connection, its own separate import binding) must be redirected for
    every connection this scenario opens to land in the isolated schema.
    """
    _require_configured_postgres()
    config = load_config()
    schema = f"authz_test_{uuid.uuid4().hex}"

    setup_conn = connect_from_config(config)
    with setup_conn.cursor() as cur:
        cur.execute(cast("LiteralString", f'CREATE SCHEMA "{schema}"'))
        cur.execute(cast("LiteralString", f'SET search_path TO "{schema}"'))
    setup_conn.commit()
    apply_pending_migrations(setup_conn, sources=STATE_MIGRATION_SOURCES)

    def _isolated_connect_from_config(cfg: object) -> psycopg.Connection[TupleRow]:
        del cfg
        conn = connect_from_config(config)
        with conn.cursor() as cur:
            cur.execute(cast("LiteralString", f'SET search_path TO "{schema}"'))
        return conn

    monkeypatch.setattr("ps_service.authz.store.connect_from_config", _isolated_connect_from_config)
    monkeypatch.setattr("ps_service.audit.store.connect_from_config", _isolated_connect_from_config)

    store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
    owner_subject = _unique_subject("floor-owner")
    admin_subject = _unique_subject("floor-admin")
    store.grant(
        actor=(admin_subject, _ACTOR_ISSUER),
        target=(owner_subject, _ACTOR_ISSUER),
        access_role=AccessRole.SYSTEM_OWNER,
    )
    store.grant(
        actor=(owner_subject, _ACTOR_ISSUER),
        target=(admin_subject, _ACTOR_ISSUER),
        access_role=AccessRole.SYSTEM_ADMIN,
    )
    assert store.count_active_system_owners() == 1

    with pytest.raises(SystemOwnerFloorViolationError):
        revoke_role(
            actor=(admin_subject, _ACTOR_ISSUER),
            target_subject=owner_subject,
            access_role="SystemOwner",
            store=store,
            issuer=_ACTOR_ISSUER,
        )

    events = _select_audit_events_for(
        setup_conn, resource_id=owner_subject, action="access_role.revoke"
    )

    with setup_conn.cursor() as cur:
        cur.execute(cast("LiteralString", f'DROP SCHEMA "{schema}" CASCADE'))
    setup_conn.commit()
    setup_conn.close()

    assert events == [
        (
            admin_subject,
            _ACTOR_ISSUER,
            "rejected",
            {"access_role": "SystemOwner", "reason_code": "system_owner_floor_violation"},
        )
    ]


@pytest.mark.postgres_live
def test_bootstrap_identity_mismatch_produces_exactly_one_rejected_bootstrap_audit_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-012, bootstrap-identity-mismatch denial (the fourth denial type): the empty-store,
    non-matching-principal branch of `bootstrap_first_owner` (unchanged since Slice 2) writes
    exactly one `outcome='rejected'` `access_role.bootstrap_rejected` audit event -- proven
    here against real Postgres for the first time.
    """
    _require_configured_postgres()
    configured_owner_subject = _unique_subject("bootstrap-owner")
    owner_issuer = _ACTOR_ISSUER
    monkeypatch.setenv("PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT", configured_owner_subject)
    monkeypatch.setenv("PS_AUTHZ_BOOTSTRAP_OWNER_ISSUER", owner_issuer)
    config = load_config()
    schema = f"authz_test_{uuid.uuid4().hex}"

    setup_conn = connect_from_config(config)
    with setup_conn.cursor() as cur:
        cur.execute(cast("LiteralString", f'CREATE SCHEMA "{schema}"'))
        cur.execute(cast("LiteralString", f'SET search_path TO "{schema}"'))
    setup_conn.commit()
    apply_pending_migrations(setup_conn, sources=STATE_MIGRATION_SOURCES)

    def _isolated_connect_from_config(cfg: object) -> psycopg.Connection[TupleRow]:
        del cfg
        conn = connect_from_config(config)
        with conn.cursor() as cur:
            cur.execute(cast("LiteralString", f'SET search_path TO "{schema}"'))
        return conn

    monkeypatch.setattr("ps_service.authz.store.connect_from_config", _isolated_connect_from_config)

    store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
    mismatched_subject = _unique_subject("bootstrap-mismatch")

    result = store.bootstrap_first_owner((mismatched_subject, owner_issuer))

    assert result == frozenset({AccessRole.AUTHENTICATED_USER})

    events = _select_audit_events_for(
        setup_conn, resource_id=mismatched_subject, action="access_role.bootstrap_rejected"
    )
    roles = _active_roles_for(setup_conn, subject=mismatched_subject)

    with setup_conn.cursor() as cur:
        cur.execute(cast("LiteralString", f'DROP SCHEMA "{schema}" CASCADE'))
    setup_conn.commit()
    setup_conn.close()

    assert roles == set()
    assert events == [
        (
            "system:bootstrap",
            "system:bootstrap",
            "rejected",
            {"reason_code": "bootstrap_identity_mismatch"},
        )
    ]


# --- CHANGES.md item 3: denial-path leak-check ----------------------------------------------


@pytest.mark.postgres_live
def test_record_standalone_connection_failure_leaks_no_host_or_port_or_driver_detail() -> None:
    """CHANGES.md item 3: the denial-path leak-check, mirroring Slice 4's query-path equivalent
    (PLAN.md:819-823's own assertion shape, applied here to `record_standalone` instead of
    `query`).

    A genuine connection failure (misconfigured host/port, refused fast rather than via DNS
    timeout) must surface as `AuditPostgresUnavailableError` with a message that never
    contains the configured host or port -- `record_standalone` raises a fixed, generic
    string, never one built from the underlying `psycopg` exception's own text.
    """
    _require_configured_postgres()
    config = load_config()
    unreachable_host = "127.0.0.1"
    unreachable_port = 59999
    broken_config = dataclasses.replace(
        config, state_postgres_host=unreachable_host, state_postgres_port=unreachable_port
    )
    audit_store = PsycopgAuditStore(broken_config)

    with pytest.raises(AuditPostgresUnavailableError) as exc_info:
        audit_store.record_standalone(
            actor_subject="leak-test-actor",
            actor_issuer=_ACTOR_ISSUER,
            action="access_role.grant",
            resource_type="principal",
            resource_id="leak-test-target",
            outcome="rejected",
            details={"access_role": "SystemAdmin", "reason_code": "access_denied"},
        )

    message = str(exc_info.value)
    assert unreachable_host not in message
    assert str(unreachable_port) not in message
    assert "psycopg" not in message.lower()


@pytest.mark.postgres_live
def test_denial_recording_connection_failure_surfaces_with_no_leak() -> None:
    """CHANGES.md item 3, service-layer half: the same connection failure, reached via the
    real `ps_service.authz.store.PsycopgAccessRoleStore.record_grant_rejected` ->
    `AuditStore.record_standalone` path, surfaces with the same no-leak guarantee.
    """
    _require_configured_postgres()
    config = load_config()
    broken_config = dataclasses.replace(
        config, state_postgres_host="127.0.0.1", state_postgres_port=59999
    )
    store = PsycopgAccessRoleStore(broken_config, audit_store=PsycopgAuditStore(broken_config))

    with pytest.raises(AuditPostgresUnavailableError) as exc_info:
        store.record_grant_rejected(
            actor=("leak-test-actor", _ACTOR_ISSUER),
            target=("leak-test-target", _ACTOR_ISSUER),
            access_role=AccessRole.SYSTEM_ADMIN,
            reason_code="access_denied",
        )

    message = str(exc_info.value)
    assert "127.0.0.1" not in message
    assert "59999" not in message
    assert message == "The audit store is temporarily unavailable."
