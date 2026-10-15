"""`postgres_live` tests: the audit integrity migration is applied by provisioning only.

The `audit_integrity` privileged migration moves `audit_events` out of `public` into the
owner-protected `audit_log` schema. It is applied only by the operator provisioning path
(admin credential), after the ordinary migrations and `graph_gateway`; the ordinary startup
runner never applies it. Every test uses a real Postgres provisioned by the real Helm init
script; nothing is mocked.

Deselected by default -- run with `uv run pytest -m postgres_live` (see
`persistence/provisioned_postgres.py`).
"""

from __future__ import annotations

import dataclasses

import pytest
from authz._fakes import FakeAccessRoleStore
from persistence.provisioned_postgres import (
    Provisioned,
    migrate_state_database,
    provision_graph_log,
)

import ps_service.authz.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers access_role.* actions
from ps_service.audit import PsycopgAuditStore
from ps_service.audit.models import AuditEventRow, AuditQueryFilters
from ps_service.authz.models import AccessRole
from ps_service.authz.service import list_audit_events

pytestmark = pytest.mark.postgres_live

_AUDIT_FILE = "0001_audit_log_insert_only_hash_chain.sql"
_ROW_FIELDS = (
    "id",
    "occurred_at",
    "actor_subject",
    "actor_issuer",
    "action",
    "resource_type",
    "resource_id",
    "outcome",
    "details",
)
_OWNER = ("owner-subject", "https://issuer.example.com/")


def _scalar(prov: Provisioned, query: str, *params: object) -> object:
    with prov.as_state() as conn:
        row = conn.execute(query, params).fetchone()  # pyright: ignore[reportArgumentType]  # test-only dynamic SQL
    assert row is not None
    return row[0]


def test_ordinary_startup_runner_never_applies_the_audit_integrity_migration(
    fresh_provisioned: Provisioned,
) -> None:
    migrate_state_database(fresh_provisioned)

    components = _scalar(
        fresh_provisioned,
        "SELECT coalesce(array_agg(DISTINCT component), '{}') FROM ps_schema_migrations",
    )
    assert "audit_integrity" not in components  # pyright: ignore[reportOperatorIssue]
    assert _scalar(fresh_provisioned, "SELECT to_regnamespace('audit_log') IS NULL") is True
    assert (
        _scalar(fresh_provisioned, "SELECT to_regclass('public.audit_events') IS NOT NULL") is True
    )


def test_provisioning_an_empty_database_applies_ordinary_then_graph_gateway_then_audit_integrity(
    fresh_provisioned: Provisioned,
) -> None:
    first = provision_graph_log(fresh_provisioned)

    assert first.audit_integrity_applied == [_AUDIT_FILE]
    assert first.graph_gateway_applied
    assert (
        _scalar(
            fresh_provisioned,
            "SELECT (SELECT max(applied_at) FROM ps_schema_migrations "
            "WHERE component IN ('audit', 'authz', 'runtime_config', 'ingestion_runs')) "
            "<= (SELECT min(applied_at) FROM ps_schema_migrations "
            "WHERE component = 'graph_gateway') "
            "AND (SELECT max(applied_at) FROM ps_schema_migrations "
            "WHERE component = 'graph_gateway') "
            "<= (SELECT min(applied_at) FROM ps_schema_migrations "
            "WHERE component = 'audit_integrity')",
        )
        is True
    )

    second = provision_graph_log(fresh_provisioned)

    assert second.ordinary_applied == []
    assert second.graph_gateway_applied == []
    assert second.audit_integrity_applied == []


def test_audit_events_moves_to_audit_log_and_the_graph_log_link_follows_it(
    fresh_provisioned: Provisioned,
) -> None:
    provision_graph_log(fresh_provisioned)

    assert _scalar(fresh_provisioned, "SELECT to_regclass('public.audit_events') IS NULL") is True
    assert (
        _scalar(fresh_provisioned, "SELECT to_regclass('audit_log.audit_events') IS NOT NULL")
        is True
    )
    assert (
        _scalar(
            fresh_provisioned,
            "SELECT confrelid::regclass::text FROM pg_constraint "
            "WHERE conrelid = 'graph_log.groups'::regclass AND contype = 'f' "
            "AND confrelid = 'audit_log.audit_events'::regclass",
        )
        == "audit_log.audit_events"
    )
    assert _scalar(
        fresh_provisioned,
        "SELECT pg_get_userbyid(relowner) FROM pg_class "
        "WHERE oid = 'audit_log.audit_events'::regclass",
    ) == (fresh_provisioned.owner_role)
    assert _scalar(
        fresh_provisioned,
        "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'audit_log'",
    ) == (fresh_provisioned.owner_role)


def test_a_standalone_audit_row_is_listed_with_the_unchanged_field_set_after_the_move(
    fresh_provisioned: Provisioned,
) -> None:
    provision_graph_log(fresh_provisioned)
    config = fresh_provisioned.state_config()
    audit_store = PsycopgAuditStore(config)
    roles = FakeAccessRoleStore(expected_owner=_OWNER)
    roles.bootstrap_first_owner(_OWNER)
    assert AccessRole.SYSTEM_OWNER in roles.active_roles_for(_OWNER)

    audit_store.record_standalone(
        actor_subject="moved-table-actor",
        actor_issuer="https://issuer.example.com/",
        action="access_role.grant",
        resource_type="principal",
        resource_id="moved-table-target",
        outcome="applied",
        details={"access_role": "SystemAdmin"},
    )
    page = list_audit_events(
        _OWNER,
        filters=AuditQueryFilters(resource_id="moved-table-target"),
        cursor=None,
        page_size=10,
        access_role_store=roles,
        audit_store=audit_store,
    )

    (row,) = page.events
    assert isinstance(row, AuditEventRow)
    assert tuple(field.name for field in dataclasses.fields(row)) == _ROW_FIELDS
    assert (row.actor_subject, row.action, row.outcome) == (
        "moved-table-actor",
        "access_role.grant",
        "applied",
    )
