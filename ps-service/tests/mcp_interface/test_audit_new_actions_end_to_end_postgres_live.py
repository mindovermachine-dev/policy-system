"""Every new audit action round-trips through real Postgres and `list_audit_events`
(`postgres_live`).

The migrations 0001 + 0002 are applied to an isolated schema. Rows of each new action (written
with the real builders and typed details models) are read back through the authorised read path
by action, by `details` filter (CELEX and instrument id) and are checked for leaked markers.
Could not be run in the authoring environment
(no Postgres); it needs a CI / dev-environment run.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

import pytest
from authz._fakes import FakeAccessRoleStore

import ps_service.authz.audit_actions  # pyright: ignore[reportUnusedImport] -- side-effect import, registers `access.gate`
import ps_service.passkey_signing.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers `passkey.enroll`
from ps_service.audit import AuditQueryFilters, PsycopgAuditStore
from ps_service.authz.service import list_audit_events
from ps_service.config import ServiceConfig, load_config
from ps_service.ingestion_runs.audit_actions import (
    IngestionRunAuditEntry,
    completion_audit_entry,
    submission_audit_entry,
)
from ps_service.invitations.audit_actions import USER_INVITE_ACTION
from ps_service.mcp_interface.audit_actions import QUERY_CYPHER_ACTION

if TYPE_CHECKING:
    from persistence.provisioned_postgres import Provisioned

_OWNER = ("owner-sub", "https://issuer.example.com/")
_ACTOR = ("actor-sub", "https://issuer.example.com/")
_CELEX = "32024R2847"
_RUN = "11111111-1111-4111-8111-111111111111"
_FAILED_RUN = "22222222-2222-4222-8222-222222222222"
_MARKERS = ("Traceback", "/Users/", "/app/", "Bearer", "itoken")


@pytest.fixture(name="live_config")
def _live_config(fresh_state_postgres_env: Provisioned) -> ServiceConfig:  # pyright: ignore[reportUnusedFunction]  # used by name
    """The real config, aimed at a brand-new cluster whose `audit_log.audit_events` is empty."""
    config = load_config()
    assert config.state_postgres_host == fresh_state_postgres_env.host
    return config


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
    # The two outcomes a restore group can end in besides success and failure
    store.record_standalone(
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        action="instrument.restore",
        resource_type="instrument",
        resource_id="DORA-1.0",
        outcome="rejected",
        details={
            "instrument_id": "DORA-1.0",
            "status": "rejected",
            "source": "upload",
            "reason_code": "group_rejected",
        },
    )
    store.record_standalone(
        actor_subject=_ACTOR[0],
        actor_issuer=_ACTOR[1],
        action="instrument.restore",
        resource_type="instrument",
        resource_id="NIS2-1.0",
        outcome="applied",
        details={
            "instrument_id": "NIS2-1.0",
            "status": "committed_apply_pending",
            "source": "catalog",
        },
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
        action=USER_INVITE_ACTION,
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
        ("instrument.restore", 3),
        ("near_miss.resolve", 1),
        (USER_INVITE_ACTION, 1),
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
def test_a_rejected_and_a_committed_apply_pending_restore_round_trip_with_their_status(
    live_config: ServiceConfig,
) -> None:
    store = PsycopgAuditStore(live_config)
    _seed(store)

    (rejected,) = _list(store, AuditQueryFilters(details={"instrument_id": "DORA-1.0"}))
    (pending,) = _list(store, AuditQueryFilters(details={"instrument_id": "NIS2-1.0"}))

    assert (rejected.outcome, rejected.details["status"], rejected.details["reason_code"]) == (
        "rejected",
        "rejected",
        "group_rejected",
    )
    assert (pending.outcome, pending.details["status"]) == ("applied", "committed_apply_pending")


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


@pytest.mark.postgres_live
def test_access_gate_passkey_enroll_and_query_cypher_rows_round_trip_with_typed_details(
    live_config: ServiceConfig,
) -> None:
    """The three actions of the role gate work are accepted by the real store and read back."""
    store = PsycopgAuditStore(live_config)
    rows: tuple[
        tuple[str, str, str, Literal["applied", "rejected", "failed"], dict[str, object]], ...
    ] = (
        (
            "access.gate",
            "operation",
            "set-catalog-source",
            "rejected",
            {"required_role": "SystemAdmin"},
        ),
        ("passkey.enroll", "pending_approval", "approval-1", "applied", {"proof": "approval_code"}),
        (QUERY_CYPHER_ACTION, "query", "cypher", "applied", {"row_count": 3, "truncated": False}),
        (QUERY_CYPHER_ACTION, "query", "cypher", "failed", {"reason_code": "query_failed"}),
    )
    for action, resource_type, resource_id, outcome, details in rows:
        store.record_standalone(
            actor_subject=_ACTOR[0],
            actor_issuer=_ACTOR[1],
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            details=details,
        )

    for action, resource_type, expected in (
        ("access.gate", "operation", 1),
        ("passkey.enroll", "pending_approval", 1),
        (QUERY_CYPHER_ACTION, "query", 2),
    ):
        by_action = _list(store, AuditQueryFilters(action=action))
        by_resource_type = _list(store, AuditQueryFilters(resource_type=resource_type))
        assert len(by_action) == expected, action
        assert {e.action for e in by_resource_type} == {action}
        assert {e.actor_subject for e in by_action} == {_ACTOR[0]}
    by_actor = _list(store, AuditQueryFilters(actor_subject=_ACTOR[0]))
    assert len(by_actor) == 4
    assert not any(marker in json.dumps(e.details) for e in by_actor for marker in _MARKERS)
