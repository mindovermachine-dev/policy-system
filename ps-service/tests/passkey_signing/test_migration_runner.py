"""Tests for `ps_service.passkey_signing.migration_runner` (issue #131, PLAN.md §0.6).

`postgres_live`-marked throughout: the runner's whole job (actually apply
`.sql` DDL, track it in `schema_migrations`, be idempotent on a repeat run)
can only be proven against a real Postgres instance -- there is no
meaningful fake for "does this SQL actually apply."
"""

from __future__ import annotations

import pytest

from ps_service.config import load_config
from ps_service.passkey_signing.migration_runner import apply_pending_migrations
from ps_service.passkey_signing.store import connect_from_config


def _require_configured_postgres() -> None:
    config = load_config()
    assert config.passkey_signing_postgres_host is not None, (
        "postgres_live requires PS_PASSKEYSIGNING_POSTGRES_HOST to be set"
    )


@pytest.mark.postgres_live
def test_apply_pending_migrations_creates_the_pending_approvals_table() -> None:
    """A first run creates `pending_approvals` and records the migration filename."""
    _require_configured_postgres()
    config = load_config()

    with connect_from_config(config) as conn:
        apply_pending_migrations(conn)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM schema_migrations WHERE filename = %(filename)s",
                {"filename": "0001_pending_approvals.sql"},
            )
            recorded = cur.fetchone() is not None

            cur.execute("SELECT to_regclass('public.pending_approvals')")
            table_exists = cur.fetchone()

    assert recorded is True
    assert table_exists is not None
    assert table_exists[0] is not None


@pytest.mark.postgres_live
def test_applying_migrations_twice_is_idempotent() -> None:
    """A second `apply_pending_migrations` call against the same database applies nothing new.

    Deliberately does not assert on the *first* call's return value: another
    test (or a prior run against the same long-lived dev database) may have
    already applied `0001_pending_approvals.sql`, which is exactly the
    idempotency this test proves -- only the second call within this test is
    guaranteed empty regardless of prior state.
    """
    _require_configured_postgres()
    config = load_config()

    with connect_from_config(config) as conn:
        apply_pending_migrations(conn)  # ensure the schema exists, regardless of run order
        second_run = apply_pending_migrations(conn)

    assert second_run == []
