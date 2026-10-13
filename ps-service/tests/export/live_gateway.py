"""The real Graph Write Gateway for Export's `falkordb_live` + `postgres_live` tests (issue #213).

Export writes its embedding backfill through the gateway, so the live tests that call
`export_instrument` need a real one: the `PsycopgGraphLogStore` as the `ps_state` role of a scratch
PS Postgres (the `provisioned_graph_log` fixture) and the real FalkorDB opener, plus an audit row
to anchor the group to. Not run in the implementation sandbox (no FalkorDB, no Postgres).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from graph_gateway.live_postgres import committed_audit_event

from ps_service.graph_gateway.connection import select_graph
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.store import PsycopgGraphLogStore

if TYPE_CHECKING:
    from collections.abc import Generator

    from falkordb import FalkorDB
    from persistence.provisioned_postgres import Provisioned


@contextmanager
def live_write_target(
    prov: Provisioned, db: FalkorDB, baseline_graph_name: str
) -> Generator[dict[str, object]]:
    """The gateway keywords (`submitter`, `audit_event_id`, `baseline_graph_name`) of the export."""
    gateway = GraphWriteGateway(
        log_store=PsycopgGraphLogStore(prov.state_config()),
        graph_opener=lambda graph: select_graph(db, graph),
    )
    try:
        yield {
            "submitter": gateway,
            "audit_event_id": committed_audit_event(prov),
            "baseline_graph_name": baseline_graph_name,
        }
    finally:
        gateway.stop_reconciler()
