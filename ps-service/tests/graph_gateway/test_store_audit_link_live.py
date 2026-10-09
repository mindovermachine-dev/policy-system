"""`postgres_live` tests for the same-transaction audit link of the graph log (issue #205, slice 4).

A caller records the audit row and appends the log group on ONE transaction; the group carries
the audit event id as a foreign key, so both commit or neither does (AC-BI-004), and the log is
retrievable by that id.

Deselected by default -- run with `uv run pytest -m postgres_live`; needs
`PS_TEST_POSTGRES_SUPERUSER_DSN` and `psql` on `PATH`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import psycopg
import pytest

import ps_service.authz.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers access_role.* actions
from ps_service.audit.store import PsycopgAuditStore
from ps_service.graph_gateway.errors import GraphLogPersistenceError
from ps_service.graph_gateway.models import GraphLogEntryDraft, GraphLogGroupDraft
from ps_service.graph_gateway.store import PsycopgGraphLogStore
from ps_service.persistence import connect_from_config

if TYPE_CHECKING:
    from persistence.provisioned_postgres import Provisioned
    from psycopg.rows import TupleRow

pytestmark = pytest.mark.postgres_live


def _graph() -> str:
    return f"graph-{uuid.uuid4().hex[:8]}"


def _group(graph: str, count: int = 2) -> GraphLogGroupDraft:
    entries = tuple(
        GraphLogEntryDraft(name="Capability", identity=f"cap-{index}", content={"n": index})
        for index in range(count)
    )
    return GraphLogGroupDraft(graph=graph, entries=entries)


def _record_audit_row(audit: PsycopgAuditStore, cur: psycopg.Cursor[TupleRow]) -> str:
    return audit.record(
        cur,
        actor_subject="test-actor-subject",
        actor_issuer="https://issuer.example.com/",
        action="access_role.grant",
        resource_type="principal",
        resource_id=f"subject-{uuid.uuid4().hex[:8]}",
        outcome="applied",
        details={"access_role": "SystemAdmin"},
    )


def _audit_row_exists(prov: Provisioned, audit_event_id: str) -> bool:
    with prov.superuser_connect(prov.state_db) as conn:
        row = conn.execute(
            "SELECT count(*) FROM audit_events WHERE id = %s", (audit_event_id,)
        ).fetchone()
    assert row is not None
    return row[0] == 1


def test_group_and_audit_row_commit_in_one_transaction_and_log_is_retrievable_by_audit_event_id(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    audit = PsycopgAuditStore(prov.state_config())
    store = PsycopgGraphLogStore(prov.state_config())
    graph = _graph()

    with connect_from_config(prov.state_config()) as conn, conn.cursor() as cur:
        audit_event_id = _record_audit_row(audit, cur)
        appended = store.append_group(cur, _group(graph), audit_event_id=audit_event_id)

    groups = store.read_groups_by_audit_event(audit_event_id)
    assert len(groups) == 1
    (found,) = groups
    assert found.group_id == appended.group_id
    assert found.audit_event_id == audit_event_id
    assert (found.graph, found.first_position, found.last_position) == (graph, 1, 2)
    assert [entry.identity for entry in found.entries] == ["cap-0", "cap-1"]
    assert [entry.position for entry in found.entries] == [1, 2]
    assert _audit_row_exists(prov, audit_event_id)


def test_one_audit_event_can_anchor_groups_of_several_graphs(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    audit = PsycopgAuditStore(prov.state_config())
    store = PsycopgGraphLogStore(prov.state_config())
    graph_a, graph_b = sorted((_graph(), _graph()))

    with connect_from_config(prov.state_config()) as conn, conn.cursor() as cur:
        audit_event_id = _record_audit_row(audit, cur)
        store.append_group(cur, _group(graph_b, 1), audit_event_id=audit_event_id)
        store.append_group(cur, _group(graph_a, 3), audit_event_id=audit_event_id)

    groups = store.read_groups_by_audit_event(audit_event_id)
    assert [(group.graph, len(group.entries)) for group in groups] == [(graph_a, 3), (graph_b, 1)]


def test_rollback_after_both_writes_removes_audit_row_and_group_together(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    audit = PsycopgAuditStore(prov.state_config())
    store = PsycopgGraphLogStore(prov.state_config())
    graph = _graph()

    with connect_from_config(prov.state_config()) as conn, conn.cursor() as cur:
        audit_event_id = _record_audit_row(audit, cur)
        store.append_group(cur, _group(graph), audit_event_id=audit_event_id)
        conn.rollback()

    assert not _audit_row_exists(prov, audit_event_id)
    assert store.read_groups_by_audit_event(audit_event_id) == ()
    assert store.last_position(graph) == 0


def test_audit_event_id_must_reference_an_existing_audit_row(
    provisioned_graph_log: Provisioned,
) -> None:
    store = PsycopgGraphLogStore(provisioned_graph_log.state_config())
    graph = _graph()

    with pytest.raises(GraphLogPersistenceError) as raised:
        store.append_group_standalone(_group(graph), audit_event_id=str(uuid.uuid4()))

    assert isinstance(raised.value.__cause__, psycopg.errors.ForeignKeyViolation)
    assert store.last_position(graph) == 0


def test_audit_row_referenced_by_a_group_cannot_be_deleted_by_state_role(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    audit = PsycopgAuditStore(prov.state_config())
    store = PsycopgGraphLogStore(prov.state_config())
    with connect_from_config(prov.state_config()) as conn, conn.cursor() as cur:
        anchored_id = _record_audit_row(audit, cur)
        store.append_group(cur, _group(_graph()), audit_event_id=anchored_id)
        unanchored_id = _record_audit_row(audit, cur)

    with prov.as_state() as state:
        state.execute("DELETE FROM audit_events WHERE id = %s", (unanchored_id,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            state.execute("DELETE FROM audit_events WHERE id = %s", (anchored_id,))

    assert _audit_row_exists(prov, anchored_id)
    assert not _audit_row_exists(prov, unanchored_id)


def test_read_groups_by_unknown_audit_event_id_returns_nothing(
    provisioned_graph_log: Provisioned,
) -> None:
    store = PsycopgGraphLogStore(provisioned_graph_log.state_config())

    assert store.read_groups_by_audit_event(str(uuid.uuid4())) == ()
