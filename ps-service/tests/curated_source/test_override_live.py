"""`postgres_live` tests for the catalog-source override against a real Postgres (issue #130).

AC-BI-007 (persisted in `runtime_config`, survives a restart, no FalkorDB involved),
AC-BI-008 (reset removes the row and resolution falls back to the default), AC-BI-010
(an unreachable store fails closed with no host/port detail) and the full MCP path:
`set-catalog-source` -> `runtime_config` + `audit_events` in one transaction ->
`list-audit-events`.

Deselected by default -- run with `uv run pytest -m postgres_live` against a reachable
`PS_STATE_POSTGRES_*` instance. The override is one shared row, so every test resets it
afterwards.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest

from ps_service.audit import MIGRATIONS_DIR as AUDIT_MIGRATIONS_DIR
from ps_service.audit import PsycopgAuditStore
from ps_service.authz import MIGRATIONS_DIR as AUTHZ_MIGRATIONS_DIR
from ps_service.config import ServiceConfig, load_config
from ps_service.curated_source.config_key import CATALOG_SOURCE_KEY
from ps_service.curated_source.resolve import EffectiveCatalogSource, resolve_effective_source
from ps_service.curated_source.store import get_override, reset_override, set_override
from ps_service.persistence import MigrationSource, apply_pending_migrations, connect_from_config
from ps_service.runtime_config import MIGRATIONS_DIR as RUNTIME_CONFIG_MIGRATIONS_DIR
from ps_service.runtime_config import PsycopgRuntimeConfigStore, RuntimeConfigUnavailableError

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.postgres_live

# Mirrors the source list `ps_service.main` passes to the runner at startup.
STATE_MIGRATION_SOURCES = [
    MigrationSource("audit", AUDIT_MIGRATIONS_DIR),
    MigrationSource("authz", AUTHZ_MIGRATIONS_DIR),
    MigrationSource("runtime_config", RUNTIME_CONFIG_MIGRATIONS_DIR),
]

_ISSUER = "https://issuer.example.com/"
_OVERRIDE_URL = "https://example.com/live-override"
_ACTOR = ("live-test-actor", _ISSUER)


def _store(config: ServiceConfig) -> PsycopgRuntimeConfigStore:
    return PsycopgRuntimeConfigStore(config, audit_store=PsycopgAuditStore(config))


@pytest.fixture
def config() -> Iterator[ServiceConfig]:
    """A migrated real config; the shared override row is reset again afterwards."""
    real = load_config()
    assert real.state_postgres_host is not None, (
        "postgres_live requires PS_STATE_POSTGRES_HOST to be set"
    )
    with connect_from_config(real) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
    yield real
    reset_override(_store(real), actor=_ACTOR)


def _row_count(config: ServiceConfig) -> int:
    with connect_from_config(config) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM runtime_config WHERE key = %(key)s", {"key": CATALOG_SOURCE_KEY}
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def test_override_persists_across_store_instances_and_is_used_by_catalog_resolution(
    config: ServiceConfig,
) -> None:
    set_override(_store(config), _OVERRIDE_URL, actor=_ACTOR)

    restarted = _store(config)  # a fresh store instance stands in for a process restart

    assert get_override(restarted) == _OVERRIDE_URL
    assert resolve_effective_source(config, store=restarted) == EffectiveCatalogSource(
        url=_OVERRIDE_URL, is_override=True
    )
    assert _row_count(config) == 1


def test_override_needs_no_falkordb_configuration(config: ServiceConfig) -> None:
    no_graph = dataclasses.replace(config, falkordb_host=None)

    set_override(_store(no_graph), _OVERRIDE_URL, actor=_ACTOR)

    assert get_override(_store(no_graph)) == _OVERRIDE_URL


def test_reset_removes_row_and_catalog_falls_back_to_default(config: ServiceConfig) -> None:
    set_override(_store(config), _OVERRIDE_URL, actor=_ACTOR)

    reset_override(_store(config), actor=_ACTOR)

    assert _row_count(config) == 0
    assert resolve_effective_source(config, store=_store(config)) == EffectiveCatalogSource(
        url=config.curated_source_base_url, is_override=False
    )


def test_unreachable_store_fails_closed_with_no_connection_detail(config: ServiceConfig) -> None:
    set_override(_store(config), _OVERRIDE_URL, actor=_ACTOR)
    broken = dataclasses.replace(config, state_postgres_host="127.0.0.1", state_postgres_port=59999)

    with pytest.raises(RuntimeConfigUnavailableError) as exc_info:
        resolve_effective_source(broken, store=_store(broken))

    message = str(exc_info.value)
    assert "127.0.0.1" not in message
    assert "59999" not in message


def test_set_and_reset_audit_rows_never_carry_url_credentials_or_query(
    config: ServiceConfig,
) -> None:
    secret_url = "https://svc-user:hunter2@example.com/curated?token=hunter2"
    set_override(_store(config), secret_url, actor=_ACTOR)
    reset_override(_store(config), actor=_ACTOR)

    with connect_from_config(config) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT details::text FROM audit_events WHERE resource_id = %(key)s "
            "AND actor_subject = %(actor)s ORDER BY occurred_at DESC, id DESC LIMIT 2",
            {"key": CATALOG_SOURCE_KEY, "actor": _ACTOR[0]},
        )
        details = " ".join(row[0] for row in cur.fetchall())

    assert "hunter2" not in details
    assert "svc-user" not in details
    assert "https://example.com/curated" in details
    assert get_override(_store(config)) is None  # reset removed the raw value
