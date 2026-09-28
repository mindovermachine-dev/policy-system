"""Tests for `ps_service.authz.migration_runner` (issue #144, PLAN.md §1 D-5).

`postgres_live`-marked throughout, mirroring
`tests/passkey_signing/test_migration_runner.py`'s own pattern: whether
`0003_bootstrap_rejected_event_type.sql` actually applies, and whether the
widened `access_role_grant_events.event_type` CHECK constraint actually
accepts `'bootstrap_rejected'` while still rejecting an arbitrary string, can
only be proven against a real Postgres instance -- there is no meaningful
fake for "does this SQL actually apply."

Deselected by default (BASELINE.md's tier gating) -- run explicitly with
`uv run pytest -m postgres_live` against a reachable `PS_AUTHZ_POSTGRES_*`
instance (e.g. `postgres:16-alpine`, matching
`psServiceSigning.postgres.image` in `charts/policy-system/values.yaml`).

Issue #147 note: migration `0005` permanently drops `access_role_grant_events`
the first time `apply_pending_migrations` is ever called against a database
with it still pending -- and `apply_pending_migrations` always cascades to
every pending file, never just "up to some point." Every test below that
needs `access_role_grant_events` to still exist at a specific migration
boundary (rather than whatever cumulative state the shared `public` schema
happens to be in, session-wide, once some *other* postgres_live test
anywhere has already reached `0005`) runs against its own throwaway Postgres
schema via `_isolated_authz_connection`, and uses `_apply_migrations_through`
(a restricted, test-only alternative to `apply_pending_migrations` that stops
at a named file) to freeze state at a chosen point before triggering 0005
for real. Tests that don't care whether `0005` has already run elsewhere in
the session (the `0004`/`audit_events` ones) keep using the shared `public`
schema via plain `connect_from_config`, unchanged.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

import psycopg
import pytest

from ps_service.authz.errors import AccessRoleMigrationApplyError
from ps_service.authz.migration_runner import (
    _discover_migration_files,  # pyright: ignore[reportPrivateUsage]  -- test-only reuse of the runner's own file-discovery, mirrors `_split_statements` import immediately below
    _split_statements,  # pyright: ignore[reportPrivateUsage]  -- test-only reuse so this file's restricted apply-through helper never duplicates the runner's own statement-splitting logic
    apply_pending_migrations,
)
from ps_service.authz.store import connect_from_config
from ps_service.config import load_config

if TYPE_CHECKING:
    from typing import LiteralString

    from psycopg.rows import TupleRow

    from ps_service.config import ServiceConfig


def _require_configured_postgres() -> None:
    config = load_config()
    assert config.authz_postgres_host is not None, (
        "postgres_live requires PS_AUTHZ_POSTGRES_HOST to be set"
    )


def _isolated_authz_connection(config: ServiceConfig) -> tuple[psycopg.Connection[TupleRow], str]:
    """Open a connection pinned (via `search_path`) to a brand-new, uniquely-named schema.

    Issue #147: migrations are strictly cumulative/irreversible within a
    schema (0005 permanently drops `access_role_grant_events`), so sharing
    one physical schema across every `postgres_live` test in the whole
    session makes "state before 0005 ran" unobservable the moment any other
    test's own `apply_pending_migrations` call reaches it first. Each caller
    of this helper gets its own private schema instead, dropped by
    `_cleanup_isolated_schema` once the test is done.

    Returns `(conn, schema_name)` -- the caller passes both back to
    `_cleanup_isolated_schema`. The schema name is a `uuid4`-generated
    identifier, never external input -- safe to interpolate directly.
    """
    conn = connect_from_config(config)
    schema = f"authz_test_{uuid.uuid4().hex}"
    with conn.cursor() as cur:
        cur.execute(cast("LiteralString", f'CREATE SCHEMA "{schema}"'))
        cur.execute(cast("LiteralString", f'SET search_path TO "{schema}"'))
    conn.commit()
    return conn, schema


def _cleanup_isolated_schema(conn: psycopg.Connection[TupleRow], schema: str) -> None:
    """Drop the schema `_isolated_authz_connection` created, then close `conn`."""
    with conn.cursor() as cur:
        cur.execute(cast("LiteralString", f'DROP SCHEMA "{schema}" CASCADE'))
    conn.commit()
    conn.close()


def _apply_migrations_through(conn: psycopg.Connection[TupleRow], last_filename: str) -> None:
    """Apply migration files up to and including `last_filename`, in filename order.

    A test-only, restricted alternative to `apply_pending_migrations` (which
    always applies *every* pending file, cascading all the way to the
    newest) -- needed by tests that must freeze database state at a specific
    migration boundary (e.g. "after 0004, before 0005") to seed
    pre-migration data before letting the real migration run. Reuses
    `migration_runner`'s own private file-discovery/statement-splitting
    helpers so the actual SQL-application logic is never duplicated; the
    per-file-transaction/rollback contract mirrors
    `apply_pending_migrations` itself.
    """
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS authz_schema_migrations ("
            "filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
    conn.commit()
    for migration_file in _discover_migration_files():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM authz_schema_migrations WHERE filename = %(filename)s",
                {"filename": migration_file.name},
            )
            already_applied = cur.fetchone() is not None
        if not already_applied:
            sql = migration_file.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                for statement in _split_statements(sql):
                    cur.execute(cast("LiteralString", statement))
                cur.execute(
                    "INSERT INTO authz_schema_migrations (filename) VALUES (%(filename)s)",
                    {"filename": migration_file.name},
                )
            conn.commit()
        if migration_file.name == last_filename:
            return


@pytest.mark.postgres_live
def test_0003_bootstrap_rejected_event_type_applies_cleanly_after_0001_and_0002() -> None:
    """A fresh (or already-migrated) database ends up with `0003` recorded as applied."""
    _require_configured_postgres()
    config = load_config()

    with connect_from_config(config) as conn:
        apply_pending_migrations(conn)  # ensure 0001/0002 exist regardless of prior run order

        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM authz_schema_migrations WHERE filename = %(filename)s",
                {"filename": "0003_bootstrap_rejected_event_type.sql"},
            )
            recorded = cur.fetchone() is not None

    assert recorded is True


@pytest.mark.postgres_live
def test_widened_constraint_accepts_bootstrap_rejected_event_type() -> None:
    """After `0003` applies, an INSERT with `event_type='bootstrap_rejected'` succeeds.

    Runs against its own isolated schema, frozen at `0003` (issue #147's
    `0005` would otherwise permanently drop this table -- see this module's
    own docstring).
    """
    _require_configured_postgres()
    config = load_config()

    conn, schema = _isolated_authz_connection(config)
    try:
        _apply_migrations_through(conn, "0003_bootstrap_rejected_event_type.sql")

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO access_role_grant_events "
                "(event_type, actor_subject, actor_issuer, target_subject, target_issuer, "
                "access_role) VALUES "
                "('bootstrap_rejected', 'system:bootstrap', 'system:bootstrap', "
                "'test-rejected-subject', 'https://issuer.example.com/', 'SystemOwner')"
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM access_role_grant_events "
                "WHERE event_type = 'bootstrap_rejected' AND target_subject = "
                "'test-rejected-subject'"
            )
            found = cur.fetchone() is not None
    finally:
        _cleanup_isolated_schema(conn, schema)

    assert found is True


@pytest.mark.postgres_live
def test_constraint_still_rejects_an_event_type_outside_the_widened_set() -> None:
    """The CHECK constraint is widened, not dropped -- an unrelated value is still rejected.

    Runs against its own isolated schema, frozen at `0003` (see this
    module's own docstring).
    """
    _require_configured_postgres()
    config = load_config()

    conn, schema = _isolated_authz_connection(config)
    try:
        _apply_migrations_through(conn, "0003_bootstrap_rejected_event_type.sql")

        with (
            pytest.raises(psycopg.errors.CheckViolation),
            conn.cursor() as cur,
        ):
            cur.execute(
                "INSERT INTO access_role_grant_events "
                "(event_type, actor_subject, actor_issuer, target_subject, target_issuer, "
                "access_role) VALUES "
                "('not_a_real_type', 'x', 'y', 'z', 'w', 'SystemOwner')"
            )
        conn.rollback()
    finally:
        _cleanup_isolated_schema(conn, schema)


@pytest.mark.postgres_live
def test_0004_audit_events_applies_cleanly_and_creates_the_expected_table() -> None:
    """`0004_audit_events.sql` applies and creates a table an ordinary insert round-trips through.

    Issue #147.

    `audit_events` is never dropped by any migration -- unlike the
    `access_role_grant_events` tests above, this one doesn't need an
    isolated schema; the shared `public` schema (whatever migration state it
    happens to be in already) is fine.
    """
    _require_configured_postgres()
    config = load_config()

    with connect_from_config(config) as conn:
        apply_pending_migrations(conn)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM authz_schema_migrations WHERE filename = %(filename)s",
                {"filename": "0004_audit_events.sql"},
            )
            recorded = cur.fetchone() is not None
        assert recorded is True

        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_events WHERE resource_id = 'migration-test-target'")
            cur.execute(
                "INSERT INTO audit_events "
                "(actor_subject, actor_issuer, action, resource_type, resource_id, outcome, "
                "details) VALUES "
                "('test-actor', 'https://issuer.example.com/', 'test.action', 'principal', "
                "'migration-test-target', 'applied', '{\"key\": \"value\"}'::jsonb)"
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT actor_subject, actor_issuer, action, resource_type, resource_id, "
                "outcome, details FROM audit_events WHERE resource_id = 'migration-test-target'"
            )
            row = cur.fetchone()

    assert row == (
        "test-actor",
        "https://issuer.example.com/",
        "test.action",
        "principal",
        "migration-test-target",
        "applied",
        {"key": "value"},
    )


@pytest.mark.postgres_live
def test_0004_audit_events_outcome_check_constraint_rejects_an_unknown_outcome() -> None:
    """`audit_events.outcome`'s `CHECK` constraint admits only `applied`/`rejected`/`failed`.

    Issue #147.
    """
    _require_configured_postgres()
    config = load_config()

    with connect_from_config(config) as conn:
        apply_pending_migrations(conn)

        with (
            pytest.raises(psycopg.errors.CheckViolation),
            conn.cursor() as cur,
        ):
            cur.execute(
                "INSERT INTO audit_events "
                "(actor_subject, actor_issuer, action, resource_type, resource_id, outcome) "
                "VALUES ('a', 'b', 'c', 'd', 'e', 'not_a_real_outcome')"
            )
        conn.rollback()


@pytest.mark.postgres_live
def test_0005_migrates_every_event_type_into_audit_events_and_drops_the_old_table() -> None:
    """AC-BI-003: one `access_role_grant_events` row of each `event_type` maps to exactly
    one `audit_events` row with the documented field mapping, and the old table is gone.

    Issue #147, Slice 2 (PLAN.md/CHANGES.md Appendix A1: `0005` lands with
    the `store.py` repoint, not Slice 1). Runs against its own isolated
    schema, frozen at `0004` before seeding legacy rows and then letting the
    real, unrestricted `apply_pending_migrations` apply `0005` for real (see
    this module's own docstring for why an isolated schema is needed here).
    """
    _require_configured_postgres()
    config = load_config()

    conn, schema = _isolated_authz_connection(config)
    try:
        _apply_migrations_through(conn, "0004_audit_events.sql")

        rows = (
            ("bootstrap", "system:bootstrap", "system:bootstrap", "target-a", "SystemOwner"),
            ("grant", "actor-a", "actor-a", "target-b", "SystemAdmin"),
            ("revoke", "actor-b", "actor-b", "target-c", "PolicyManager"),
            (
                "bootstrap_rejected",
                "system:bootstrap",
                "system:bootstrap",
                "target-d",
                "SystemOwner",
            ),
        )
        with conn.cursor() as cur:
            for event_type, actor_subject, actor_issuer, target_subject, access_role in rows:
                cur.execute(
                    "INSERT INTO access_role_grant_events "
                    "(event_type, actor_subject, actor_issuer, target_subject, target_issuer, "
                    "access_role) VALUES (%(event_type)s, %(actor_subject)s, %(actor_issuer)s, "
                    "%(target_subject)s, 'https://issuer.example.com/', %(access_role)s)",
                    {
                        "event_type": event_type,
                        "actor_subject": actor_subject,
                        "actor_issuer": actor_issuer,
                        "target_subject": target_subject,
                        "access_role": access_role,
                    },
                )
        conn.commit()

        applied = apply_pending_migrations(conn)  # only 0005 is pending in this fresh schema

        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, resource_type, resource_id, actor_subject, actor_issuer, "
                "outcome, details FROM audit_events WHERE resource_id LIKE 'target-%' "
                "ORDER BY resource_id"
            )
            migrated = cur.fetchall()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = "
                "current_schema() AND table_name = 'access_role_grant_events'"
            )
            table_exists_after = cur.fetchone() is not None
    finally:
        _cleanup_isolated_schema(conn, schema)

    assert applied == ["0005_migrate_access_role_grant_events_to_audit_events.sql"]
    assert table_exists_after is False
    assert migrated == [
        (
            "access_role.bootstrap",
            "principal",
            "target-a",
            "system:bootstrap",
            "system:bootstrap",
            "applied",
            {"access_role": "SystemOwner"},
        ),
        (
            "access_role.grant",
            "principal",
            "target-b",
            "actor-a",
            "actor-a",
            "applied",
            {"access_role": "SystemAdmin"},
        ),
        (
            "access_role.revoke",
            "principal",
            "target-c",
            "actor-b",
            "actor-b",
            "applied",
            {"access_role": "PolicyManager"},
        ),
        (
            "access_role.bootstrap_rejected",
            "principal",
            "target-d",
            "system:bootstrap",
            "system:bootstrap",
            "rejected",
            {"access_role": "SystemOwner"},
        ),
    ]


@pytest.mark.postgres_live
def test_0005_failure_partway_leaves_both_tables_in_their_pre_migration_state() -> None:
    """AC-BI-004: a mid-`INSERT...SELECT` failure rolls back 0005 entirely --
    `access_role_grant_events` and its rows survive, and `audit_events` holds
    none of the rows that migration would have copied.

    Simulated via a primary-key collision: a row is pre-inserted into
    `audit_events` using the *same* `id` as the `access_role_grant_events`
    row about to be migrated, so the `INSERT...SELECT` hits a `PRIMARY KEY`
    violation partway through -- `apply_pending_migrations` catches it and
    rolls back the whole file (`migration_runner.py`'s single
    per-file-transaction contract). Runs against its own isolated schema
    (see this module's own docstring).
    """
    _require_configured_postgres()
    config = load_config()

    conn, schema = _isolated_authz_connection(config)
    try:
        _apply_migrations_through(conn, "0004_audit_events.sql")

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO access_role_grant_events "
                "(event_type, actor_subject, actor_issuer, target_subject, target_issuer, "
                "access_role) VALUES ('grant', 'actor-x', 'actor-x', 'ac-bi-004-target', "
                "'https://issuer.example.com/', 'SystemAdmin') RETURNING id"
            )
            record = cur.fetchone()
            assert record is not None
            colliding_id = record[0]

            cur.execute(
                "INSERT INTO audit_events "
                "(id, actor_subject, actor_issuer, action, resource_type, resource_id, "
                "outcome, details) VALUES (%(id)s, 'pre-existing', 'pre-existing', "
                "'pre.existing', 'principal', 'pre-existing-target', 'applied', '{}'::jsonb)",
                {"id": colliding_id},
            )
        conn.commit()

        with pytest.raises(AccessRoleMigrationApplyError):
            apply_pending_migrations(conn)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM access_role_grant_events WHERE target_subject = 'ac-bi-004-target'"
            )
            grant_event_survived = cur.fetchone() is not None
            cur.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = "
                "current_schema() AND table_name = 'access_role_grant_events'"
            )
            table_still_exists = cur.fetchone() is not None
            cur.execute(
                "SELECT 1 FROM authz_schema_migrations WHERE filename = "
                "'0005_migrate_access_role_grant_events_to_audit_events.sql'"
            )
            migration_recorded = cur.fetchone() is not None
            cur.execute("SELECT id FROM audit_events")
            audit_event_ids = {record[0] for record in cur.fetchall()}
    finally:
        _cleanup_isolated_schema(conn, schema)

    assert table_still_exists is True
    assert grant_event_survived is True
    assert migration_recorded is False
    # Only the pre-inserted colliding row is present -- none of the *copied*
    # rows landed (AC-BI-004's exact wording).
    assert audit_event_ids == {colliding_id}
