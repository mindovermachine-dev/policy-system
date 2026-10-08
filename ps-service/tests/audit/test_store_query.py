"""Tests for `ps_service.audit.store.PsycopgAuditStore.query` (issue #147, Slice 4).

`postgres_live`-marked throughout, mirroring `test_store_record.py`'s own
marker/skip convention: whether newest-first ordering, per-filter
correctness, keyset pagination (no duplicate/skipped rows across pages), and
the AC-BI-011 no-leak guarantee actually hold can only be proven against a
real Postgres instance.

Deselected by default (BASELINE.md's tier gating) -- run explicitly with
`uv run pytest -m postgres_live` against a reachable `PS_STATE_POSTGRES_*`
instance.
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from typing import TYPE_CHECKING

import pytest

import ps_service.authz.audit_actions  # pyright: ignore[reportUnusedImport] -- side-effect import, registers access_role.* actions/`"principal"` resource type
import ps_service.ingestion_runs.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers ingestion_run.* actions used by the details-filter tests
from ps_service.audit import MIGRATIONS_DIR as AUDIT_MIGRATIONS_DIR
from ps_service.audit.errors import AuditPostgresUnavailableError
from ps_service.audit.models import AuditQueryFilters
from ps_service.audit.store import AuditStore, PsycopgAuditStore
from ps_service.authz import MIGRATIONS_DIR as AUTHZ_MIGRATIONS_DIR
from ps_service.config import load_config
from ps_service.persistence import MigrationSource, apply_pending_migrations, connect_from_config

if TYPE_CHECKING:
    from datetime import datetime


# Mirrors the source list `ps_service.main` passes to the runner at startup.
STATE_MIGRATION_SOURCES = [
    MigrationSource("audit", AUDIT_MIGRATIONS_DIR),
    MigrationSource("authz", AUTHZ_MIGRATIONS_DIR),
]

_ACTOR_ISSUER = "https://issuer.example.com/"
# A small, deliberate delay between seeded inserts (each its own transaction,
# so each gets its own `now()`) -- guarantees measurably distinct
# `occurred_at` values for newest-first-ordering/time-range assertions,
# without depending on sub-millisecond clock resolution alone.
_SEED_DELAY_SECONDS = 0.02


def _require_configured_postgres() -> None:
    config = load_config()
    assert config.state_postgres_host is not None, (
        "postgres_live requires PS_STATE_POSTGRES_HOST to be set"
    )


def _insert_event(
    store: AuditStore,
    config: object,
    *,
    actor_subject: str,
    resource_id: str,
    action: str,
    outcome: str = "applied",
    details: dict[str, object] | None = None,
) -> None:
    """Insert one row via the real `AuditStore.record`, committed in its own transaction
    (so it gets its own, later `now()` than any row inserted before it).
    """
    with connect_from_config(config) as conn, conn.cursor() as cur:  # pyright: ignore[reportArgumentType] -- `config` is a `ServiceConfig`, typed loosely here only to keep this helper import-light
        store.record(
            cur,
            actor_subject=actor_subject,
            actor_issuer=_ACTOR_ISSUER,
            action=action,
            resource_type="principal",
            resource_id=resource_id,
            outcome=outcome,  # pyright: ignore[reportArgumentType] -- always one of the three literal outcomes at every call site below
            details=details or {"access_role": "SystemAdmin"},
        )
        conn.commit()
    time.sleep(_SEED_DELAY_SECONDS)


@pytest.mark.postgres_live
def test_query_returns_matching_events_newest_first_and_paginates_without_gaps_or_dupes() -> None:
    """AC-BI-007: filters combine correctly, results are newest first, and a `page_size`
    smaller than the result set produces a `next_cursor` that advances through every
    remaining row exactly once (no duplicates, no skips).
    """
    _require_configured_postgres()
    config = load_config()
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
    store: AuditStore = PsycopgAuditStore(config)

    marker_actor = f"query-actor-{uuid.uuid4().hex[:10]}"
    other_actor = f"query-actor-other-{uuid.uuid4().hex[:10]}"
    resource_a = f"query-resource-a-{uuid.uuid4().hex[:10]}"
    resource_b = f"query-resource-b-{uuid.uuid4().hex[:10]}"

    # Inserted oldest-first; newest-first query results should read r3, r2, r1.
    _insert_event(
        store,
        config,
        actor_subject=marker_actor,
        resource_id=resource_a,
        action="access_role.grant",
    )
    _insert_event(
        store,
        config,
        actor_subject=marker_actor,
        resource_id=resource_a,
        action="access_role.revoke",
    )
    _insert_event(
        store,
        config,
        actor_subject=marker_actor,
        resource_id=resource_b,
        action="access_role.grant",
    )
    # Noise: a different actor, must never appear in any `marker_actor`-filtered result.
    _insert_event(
        store, config, actor_subject=other_actor, resource_id=resource_a, action="access_role.grant"
    )

    # -- actor_subject filter alone: exactly the three marker_actor rows, newest first.
    all_page = store.query(
        filters=AuditQueryFilters(actor_subject=marker_actor), cursor=None, page_size=10
    )
    assert [event.resource_id for event in all_page.events] == [
        resource_b,
        resource_a,
        resource_a,
    ]
    assert [event.action for event in all_page.events] == [
        "access_role.grant",
        "access_role.revoke",
        "access_role.grant",
    ]
    assert all(event.actor_subject == marker_actor for event in all_page.events)
    assert all_page.next_cursor is None

    # -- combined with resource_id: only the two resource_a rows.
    resource_a_page = store.query(
        filters=AuditQueryFilters(actor_subject=marker_actor, resource_id=resource_a),
        cursor=None,
        page_size=10,
    )
    assert [event.action for event in resource_a_page.events] == [
        "access_role.revoke",
        "access_role.grant",
    ]

    # -- combined with action: only the two grant rows (resource_b then the older resource_a).
    grant_page = store.query(
        filters=AuditQueryFilters(actor_subject=marker_actor, action="access_role.grant"),
        cursor=None,
        page_size=10,
    )
    assert [event.resource_id for event in grant_page.events] == [resource_b, resource_a]

    # -- pagination: page_size=1 across the three marker_actor rows advances with no
    # duplicate/skipped rows and a `None` cursor only once none remain.
    seen_ids: list[str] = []
    cursor: str | None = None
    for _ in range(3):
        page = store.query(
            filters=AuditQueryFilters(actor_subject=marker_actor), cursor=cursor, page_size=1
        )
        assert len(page.events) == 1
        seen_ids.append(page.events[0].id)
        cursor = page.next_cursor
    assert cursor is None  # no page 4 -- exactly three rows existed
    assert seen_ids == [event.id for event in all_page.events]  # same order, no gaps/dupes
    assert len(set(seen_ids)) == len(seen_ids)


@pytest.mark.postgres_live
def test_query_time_range_filter_excludes_events_outside_the_bound() -> None:
    """AC-BI-007: `occurred_from`/`occurred_to` narrow to the events in that window."""
    _require_configured_postgres()
    config = load_config()
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
    store: AuditStore = PsycopgAuditStore(config)

    marker_actor = f"query-time-actor-{uuid.uuid4().hex[:10]}"
    resource_id = f"query-time-resource-{uuid.uuid4().hex[:10]}"

    _insert_event(
        store,
        config,
        actor_subject=marker_actor,
        resource_id=resource_id,
        action="access_role.grant",
    )
    all_before = store.query(
        filters=AuditQueryFilters(actor_subject=marker_actor), cursor=None, page_size=10
    )
    boundary: datetime = all_before.events[0].occurred_at
    time.sleep(_SEED_DELAY_SECONDS)
    _insert_event(
        store,
        config,
        actor_subject=marker_actor,
        resource_id=resource_id,
        action="access_role.revoke",
    )

    only_after = store.query(
        filters=AuditQueryFilters(actor_subject=marker_actor, occurred_from=boundary),
        cursor=None,
        page_size=10,
    )
    only_before = store.query(
        filters=AuditQueryFilters(actor_subject=marker_actor, occurred_to=boundary),
        cursor=None,
        page_size=10,
    )

    assert [event.action for event in only_after.events] == [
        "access_role.revoke",
        "access_role.grant",
    ]
    assert [event.action for event in only_before.events] == ["access_role.grant"]


@pytest.mark.postgres_live
def test_query_connection_failure_leaks_no_host_or_port_or_driver_detail() -> None:
    """AC-BI-011: a genuine connection failure surfaces as `AuditPostgresUnavailableError`
    with a fixed, generic message -- never one containing the configured host/port,
    mirroring `test_store_transactional_audit.py`'s own `record_standalone` leak-check.
    """
    _require_configured_postgres()
    config = load_config()
    unreachable_host = "127.0.0.1"
    unreachable_port = 59999
    broken_config = dataclasses.replace(
        config, state_postgres_host=unreachable_host, state_postgres_port=unreachable_port
    )
    store: AuditStore = PsycopgAuditStore(broken_config)

    with pytest.raises(AuditPostgresUnavailableError) as exc_info:
        store.query(filters=AuditQueryFilters(), cursor=None, page_size=10)

    message = str(exc_info.value)
    assert unreachable_host not in message
    assert str(unreachable_port) not in message
    assert message == "The audit store is temporarily unavailable."


# --- issue #195, Slice 11: the allow-listed `details` filter (real Postgres) ---


def _store_with_migrations() -> tuple[AuditStore, object]:
    config = load_config()
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
    return PsycopgAuditStore(config), config


def _seed_submit(store: AuditStore, *, run_id: str, celex: str, actor: str) -> None:
    store.record_standalone(
        actor_subject=actor,
        actor_issuer=_ACTOR_ISSUER,
        action="ingestion_run.submit",
        resource_type="ingestion_run",
        resource_id=run_id,
        outcome="applied",
        details={
            "celex": celex,
            "short_name": "x",
            "status": "started",
            "trigger": "sync_ingest",
        },
    )


@pytest.mark.postgres_live
def test_query_details_celex_returns_only_rows_whose_details_celex_matches() -> None:
    _require_configured_postgres()
    store, _config = _store_with_migrations()
    actor = f"details-actor-{uuid.uuid4().hex[:10]}"
    _seed_submit(store, run_id="run-a", celex="32024R2847", actor=actor)
    _seed_submit(store, run_id="run-b", celex="32016R0679", actor=actor)

    page = store.query(
        filters=AuditQueryFilters(actor_subject=actor, details={"celex": "32024R2847"}),
        cursor=None,
        page_size=10,
    )

    assert [event.resource_id for event in page.events] == ["run-a"]


@pytest.mark.postgres_live
def test_query_details_filter_combines_with_action_and_time_range_using_and() -> None:
    _require_configured_postgres()
    store, _config = _store_with_migrations()
    actor = f"details-actor-{uuid.uuid4().hex[:10]}"
    _seed_submit(store, run_id="run-a", celex="32024R2847", actor=actor)

    matching = store.query(
        filters=AuditQueryFilters(
            actor_subject=actor, action="ingestion_run.submit", details={"celex": "32024R2847"}
        ),
        cursor=None,
        page_size=10,
    )
    other_action = store.query(
        filters=AuditQueryFilters(
            actor_subject=actor, action="ingestion_run.complete", details={"celex": "32024R2847"}
        ),
        cursor=None,
        page_size=10,
    )

    assert len(matching.events) == 1
    assert other_action.events == ()


@pytest.mark.postgres_live
def test_query_details_filter_value_is_parameterized_not_interpolated() -> None:
    _require_configured_postgres()
    store, _config = _store_with_migrations()
    actor = f"details-actor-{uuid.uuid4().hex[:10]}"
    _seed_submit(store, run_id="run-a", celex="32024R2847", actor=actor)

    page = store.query(
        filters=AuditQueryFilters(actor_subject=actor, details={"celex": "x' OR '1'='1"}),
        cursor=None,
        page_size=10,
    )

    assert page.events == ()


@pytest.mark.postgres_live
def test_query_details_filter_uses_the_expression_index() -> None:
    _require_configured_postgres()
    config = load_config()
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
        with conn.cursor() as cur:
            cur.execute("SET enable_seqscan = off")
            cur.execute("EXPLAIN SELECT id FROM audit_events WHERE (details ->> 'celex') = 'x'")
            plan = "\n".join(str(row[0]) for row in cur.fetchall())

    assert "audit_events_details_celex_idx" in plan
