"""`postgres_live` tests for a graph-log group with no audit link (issue #211, S1).

Policy Lifecycle's draft edits and status/version backfills have no `audit_events` row; their
groups are appended with a null `audit_event_id` (the column is nullable). Written against the
real Postgres store; NOT run in the sandbox that built it (no Postgres): unverified until run.

Deselected by default -- run with `uv run pytest -m postgres_live`; needs
`PS_TEST_POSTGRES_SUPERUSER_DSN` and `psql` on `PATH`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from ps_service.graph_gateway.models import GraphLogEntryDraft, GraphLogGroupDraft
from ps_service.graph_gateway.store import PsycopgGraphLogStore

if TYPE_CHECKING:
    from persistence.provisioned_postgres import Provisioned

pytestmark = pytest.mark.postgres_live


def _group(graph: str) -> GraphLogGroupDraft:
    return GraphLogGroupDraft(
        graph=graph,
        entries=(GraphLogEntryDraft(name="Policy", identity="p-1", content={"n": 1}),),
    )


def test_a_standalone_group_with_no_audit_event_is_logged_with_a_null_link(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = PsycopgGraphLogStore(prov.state_config())
    graph = f"graph-{uuid.uuid4().hex[:8]}"

    appended = store.append_group_standalone(_group(graph), audit_event_id=None)

    assert (appended.first_position, appended.last_position) == (1, 1)
    assert store.last_position(graph) == 1
    with prov.superuser_connect(prov.state_db) as conn:
        row = conn.execute(
            "SELECT audit_event_id FROM graph_log.groups WHERE graph = %s", (graph,)
        ).fetchone()
    assert row is not None
    assert row[0] is None


def test_a_group_with_no_audit_event_is_not_found_by_any_audit_event_id(
    provisioned_graph_log: Provisioned,
) -> None:
    store = PsycopgGraphLogStore(provisioned_graph_log.state_config())
    store.append_group_standalone(_group(f"graph-{uuid.uuid4().hex[:8]}"), audit_event_id=None)

    assert store.read_groups_by_audit_event(str(uuid.uuid4())) == ()
