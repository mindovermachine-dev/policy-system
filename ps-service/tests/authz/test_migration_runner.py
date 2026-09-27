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
"""

from __future__ import annotations

import psycopg
import pytest

from ps_service.authz.migration_runner import apply_pending_migrations
from ps_service.authz.store import connect_from_config
from ps_service.config import load_config


def _require_configured_postgres() -> None:
    config = load_config()
    assert config.authz_postgres_host is not None, (
        "postgres_live requires PS_AUTHZ_POSTGRES_HOST to be set"
    )


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
    """After `0003` applies, an INSERT with `event_type='bootstrap_rejected'` succeeds."""
    _require_configured_postgres()
    config = load_config()

    with connect_from_config(config) as conn:
        apply_pending_migrations(conn)

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

    assert found is True


@pytest.mark.postgres_live
def test_constraint_still_rejects_an_event_type_outside_the_widened_set() -> None:
    """The CHECK constraint is widened, not dropped -- an unrelated value is still rejected."""
    _require_configured_postgres()
    config = load_config()

    with connect_from_config(config) as conn:
        apply_pending_migrations(conn)

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
