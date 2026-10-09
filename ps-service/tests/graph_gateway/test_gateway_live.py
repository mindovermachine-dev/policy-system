"""`postgres_live` tests: the gateway over the real graph log store (issue #206).

The log store is the real `PsycopgGraphLogStore` as the `ps_state` role against a scratch PS
Postgres (see `persistence/provisioned_postgres.py`); the graph is the in-memory fake, so these
prove the Postgres side only: serialization across the advisory lock (AC-BI-006) and, in S7a,
the same-transaction audit link. The FalkorDB side lives in `test_gateway_falkordb_live.py`.

Deselected by default -- run with `uv run pytest -m postgres_live`; needs
`PS_TEST_POSTGRES_SUPERUSER_DSN` and `psql` on `PATH`. Not runnable in the implementation sandbox.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from itertools import groupby
from typing import TYPE_CHECKING

import pytest

import ps_service.authz.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers access_role.* actions
from graph_gateway._fakes import InMemoryGraphs
from ps_service.audit.store import PsycopgAuditStore
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.models import MutationGroup, UpsertNode
from ps_service.graph_gateway.store import PsycopgGraphLogStore
from ps_service.persistence import connect_from_config

if TYPE_CHECKING:
    import psycopg
    from persistence.provisioned_postgres import Provisioned
    from psycopg.rows import TupleRow

pytestmark = pytest.mark.postgres_live

_WAIT_SECONDS = 10.0
_BLOCKED_PROBE_SECONDS = 0.5
_WORKERS = 6
_GROUPS_PER_WORKER = 4
_NODES_PER_GROUP = 3


def _graph() -> str:
    return f"graph-{uuid.uuid4().hex[:8]}"


def _record_audit(prov: Provisioned, cur: psycopg.Cursor[TupleRow]) -> str:
    """Record an audit row on `cur` (the caller's transaction) and return its id."""
    return PsycopgAuditStore(prov.state_config()).record(
        cur,
        actor_subject="test-actor-subject",
        actor_issuer="https://issuer.example.com/",
        action="access_role.grant",
        resource_type="principal",
        resource_id=f"subject-{uuid.uuid4().hex[:8]}",
        outcome="applied",
        details={"access_role": "SystemAdmin"},
    )


def _committed_audit_event(prov: Provisioned) -> str:
    """Record and commit one audit row, so a standalone append can reference it."""
    with connect_from_config(prov.state_config()) as conn, conn.cursor() as cur:
        return _record_audit(prov, cur)


def _gateway(
    prov: Provisioned, graphs: InMemoryGraphs
) -> tuple[GraphWriteGateway, PsycopgGraphLogStore]:
    store = PsycopgGraphLogStore(prov.state_config())
    return GraphWriteGateway(log_store=store, graph_opener=graphs.open), store


def test_concurrent_groups_on_one_graph_do_not_interleave_and_apply_in_position_order(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    graph = _graph()
    graphs = InMemoryGraphs()
    gateway, store = _gateway(prov, graphs)
    audit_event_ids = [_committed_audit_event(prov) for _ in range(_WORKERS * _GROUPS_PER_WORKER)]

    def submit(index: int) -> None:
        gateway.submit_group(
            MutationGroup(
                graph=graph,
                audit_event_id=audit_event_ids[index],
                primitives=tuple(
                    UpsertNode(label="Capability", id=f"g{index}-n{node}")
                    for node in range(_NODES_PER_GROUP)
                ),
            )
        )

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        for future in [pool.submit(submit, index) for index in range(len(audit_event_ids))]:
            future.result()

    entries = store.read_entries(graph)
    assert [entry.position for entry in entries] == list(range(1, len(audit_event_ids) * 3 + 1))
    runs = [group_id for group_id, _ in groupby(entry.group_id for entry in entries)]
    assert len(runs) == len(set(runs)) == len(audit_event_ids)  # one contiguous run per group
    assert graphs.open(graph).upsert_order == [("Capability", entry.identity) for entry in entries]
    assert store.read_applied_position(graph) == store.last_position(graph) == len(entries)


def test_groups_on_different_graphs_each_get_their_own_sequence_from_one(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    graphs = InMemoryGraphs()
    gateway, store = _gateway(prov, graphs)
    first, second = _graph(), _graph()

    for graph in (first, second):
        gateway.submit_group(
            MutationGroup(
                graph=graph,
                audit_event_id=_committed_audit_event(prov),
                primitives=(UpsertNode(label="Capability", id="c"),),
            )
        )

    assert (store.last_position(first), store.last_position(second)) == (1, 1)


def _node_group(graph: str, audit_event_id: str, node_id: str) -> MutationGroup:
    return MutationGroup(
        graph=graph,
        audit_event_id=audit_event_id,
        primitives=(UpsertNode(label="Capability", id=node_id),),
    )


def test_in_transaction_submit_links_audit_row_and_group_atomically_live(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    graphs = InMemoryGraphs()
    gateway, store = _gateway(prov, graphs)
    graph = _graph()

    with connect_from_config(prov.state_config()) as conn, conn.cursor() as cur:
        rolled_back_audit = _record_audit(prov, cur)
        with gateway.submit_group_in_transaction(
            cur, _node_group(graph, rolled_back_audit, "rolled-back")
        ) as staged:
            conn.rollback()
            staged.abort()
    assert store.read_groups_by_audit_event(rolled_back_audit) == ()
    assert store.last_position(graph) == 0

    with connect_from_config(prov.state_config()) as conn, conn.cursor() as cur:
        audit_event_id = _record_audit(prov, cur)
        with gateway.submit_group_in_transaction(
            cur, _node_group(graph, audit_event_id, "committed")
        ) as staged:
            conn.commit()
            outcome = staged.complete()

    (logged,) = store.read_groups_by_audit_event(audit_event_id)
    assert (logged.first_position, logged.last_position) == (1, 1)
    assert outcome.status == "applied"
    assert set(graphs.open(graph).nodes) == {("Capability", "committed")}
    assert store.read_applied_position(graph) == 1


def test_in_transaction_submission_holds_the_graph_lock_until_complete_live(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    graphs = InMemoryGraphs()
    gateway, store = _gateway(prov, graphs)
    graph = _graph()
    other_audit = _committed_audit_event(prov)
    finished = threading.Event()

    def submit_other() -> None:
        gateway.submit_group(_node_group(graph, other_audit, "other"))
        finished.set()

    with connect_from_config(prov.state_config()) as conn, conn.cursor() as cur:
        audit_event_id = _record_audit(prov, cur)
        with gateway.submit_group_in_transaction(
            cur, _node_group(graph, audit_event_id, "staged")
        ) as staged:
            threading.Thread(target=submit_other, daemon=True).start()
            blocked = not finished.wait(_BLOCKED_PROBE_SECONDS)
            conn.commit()
            staged.complete()

    assert blocked
    assert finished.wait(_WAIT_SECONDS)
    assert [entry.identity for entry in store.read_entries(graph)] == ["staged", "other"]
