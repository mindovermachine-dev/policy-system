"""Every #195 action round-trips through a real Postgres and `list_audit_events` (`postgres_live`).

The migrations 0001 + 0002 are applied to an isolated schema. Rows of each new action (written
with the real builders and typed details models) are read back through the authorised read path
by action, by `details` filter (CELEX and instrument id) and are checked for leaked markers
(AC-BI-003 / AC-BI-009 / AC-BI-010 / AC-BI-018). Could not be run in the authoring environment
(no Postgres); it needs a CI / dev-environment run.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, cast

import pytest
from authz._fakes import FakeAccessRoleStore

from ps_service.audit import MIGRATIONS_DIR as AUDIT_MIGRATIONS_DIR
from ps_service.audit import AuditQueryFilters, PsycopgAuditStore
from ps_service.authz.service import list_audit_events
from ps_service.config import ServiceConfig, load_config
from ps_service.ingestion_runs.audit_actions import (
    IngestionRunAuditEntry,
    completion_audit_entry,
    submission_audit_entry,
)
from ps_service.persistence import MigrationSource, apply_pending_migrations, connect_from_config

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import LiteralString

_OWNER = ("owner-sub", "https://issuer.example.com/")
_ACTOR = ("actor-sub", "https://issuer.example.com/")
_CELEX = "32024R2847"
_RUN = "11111111-1111-4111-8111-111111111111"
_FAILED_RUN = "22222222-2222-4222-8222-222222222222"
_MARKERS = ("Traceback", "/Users/", "/app/", "Bearer", "itoken")


@pytest.fixture(name="live_config")
def _live_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[ServiceConfig]:  # pyright: ignore[reportUnusedFunction]  # used by name
    config = load_config()
    assert config.state_postgres_host is not None, (
        "postgres_live requires PS_STATE_POSTGRES_HOST to be set"
    )
    schema = f"audit_e2e_{uuid.uuid4().hex}"
    with connect_from_config(config) as conn:
        conn.execute(cast("LiteralString", f'CREATE SCHEMA "{schema}"'))
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=[MigrationSource("audit", AUDIT_MIGRATIONS_DIR)])
    try:
        yield config
    finally:
        monkeypatch.delenv("PGOPTIONS")
        with connect_from_config(config) as conn:
            conn.execute(cast("LiteralString", f'DROP SCHEMA "{schema}" CASCADE'))


def _write(
    store: PsycopgAuditStore, resource_type: str, resource_id: str, entry: IngestionRunAuditEntry
) -> None:
    store.record_standalone(
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        action=entry.action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=entry.outcome,
        details=entry.details,
    )


def _seed(store: PsycopgAuditStore) -> None:
    _write(
        store,
        "ingestion_run",
        _RUN,
        submission_audit_entry(celex=_CELEX, short_name="CRA", trigger="sync_ingest"),
    )
    _write(
        store,
        "ingestion_run",
        _RUN,
        completion_audit_entry(
            status="succeeded",
            celex=_CELEX,
            trigger="sync_ingest",
            result={"regulatory_instrument_id": "CRA-1.0", "outcome": "fresh", "stages": []},
            reason_code=None,
        ),
    )
    _write(
        store,
        "ingestion_run",
        _FAILED_RUN,
        completion_audit_entry(
            status="failed",
            celex="32022R2554",
            trigger="amendment_check",
            result=None,
            reason_code="pipeline_stage_failed",
        ),
    )
    store.record_standalone(
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        action="instrument.restore",
        resource_type="instrument",
        resource_id="CRA-1.0",
        outcome="applied",
        details={"instrument_id": "CRA-1.0", "status": "started", "source": "catalog"},
    )
    store.record_standalone(
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        action="near_miss.resolve",
        resource_type="near_miss_review",
        resource_id="review_aaa",
        outcome="applied",
        details={
            "review_id": "review_aaa",
            "kind": "Capability",
            "incoming_id": "cap-1",
            "existing_id": "cap-2",
            "decision": "keep_separate",
        },
    )
    store.record_standalone(
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        action="user.invite",
        resource_type="user",
        resource_id="target@example.com",
        outcome="applied",
        details={"invitee_email": "target@example.com"},
    )


def _list(store: PsycopgAuditStore, filters: AuditQueryFilters):
    roles = FakeAccessRoleStore(expected_owner=_OWNER)
    roles.bootstrap_first_owner(_OWNER)
    return list_audit_events(
        _OWNER,
        filters=filters,
        cursor=None,
        page_size=50,
        access_role_store=roles,
        audit_store=store,
    ).events


@pytest.mark.postgres_live
def test_every_new_action_row_round_trips_through_list_audit_events_with_typed_details(
    live_config: ServiceConfig,
) -> None:
    store = PsycopgAuditStore(live_config)
    _seed(store)

    for action, expected in (
        ("ingestion_run.submit", 1),
        ("ingestion_run.complete", 2),
        ("instrument.restore", 1),
        ("near_miss.resolve", 1),
        ("user.invite", 1),
    ):
        events = _list(store, AuditQueryFilters(action=action))
        assert len(events) == expected, action
        assert {e.actor_subject for e in events} == {_ACTOR[0]}


@pytest.mark.postgres_live
def test_who_ingested_instrument_x_is_answerable_by_celex_and_by_instrument_id(
    live_config: ServiceConfig,
) -> None:
    store = PsycopgAuditStore(live_config)
    _seed(store)

    by_celex = _list(store, AuditQueryFilters(details={"celex": _CELEX}))
    by_instrument = _list(store, AuditQueryFilters(details={"regulatory_instrument_id": "CRA-1.0"}))
    restores = _list(store, AuditQueryFilters(details={"instrument_id": "CRA-1.0"}))

    assert {e.resource_id for e in by_celex} == {_RUN}
    assert len(by_celex) == 2
    assert [e.action for e in by_instrument] == ["ingestion_run.complete"]
    assert [e.action for e in restores] == ["instrument.restore"]


@pytest.mark.postgres_live
def test_audit_rows_for_a_failed_ingestion_contain_no_secret_or_path_markers(
    live_config: ServiceConfig,
) -> None:
    store = PsycopgAuditStore(live_config)
    _seed(store)

    events = _list(store, AuditQueryFilters(resource_id=_FAILED_RUN))

    (failed,) = events
    assert failed.outcome == "failed"
    assert failed.details["reason_code"] == "pipeline_stage_failed"
    text = json.dumps(failed.details)
    assert not any(marker in text for marker in _MARKERS)
