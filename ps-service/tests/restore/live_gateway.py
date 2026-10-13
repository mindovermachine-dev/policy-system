"""The real Graph Write Gateway for Restore's `falkordb_live` + `postgres_live` tests (issue #214).

Restore writes its three graphs through the gateway, so the live tests that call
`restore_instrument` need a real one: the `PsycopgGraphLogStore` as the `ps_state` role of a
scratch PS Postgres (the `provisioned_graph_log` fixture) and the real FalkorDB opener, plus an
audit row to anchor the restore's groups to. Modelled on `export.live_gateway`. Not run in the
implementation sandbox (no FalkorDB, no Postgres).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from graph_gateway.live_postgres import committed_audit_event

from ps_service.graph_gateway.connection import select_graph
from ps_service.graph_gateway.default_gateway import FalkorDBKeyPort
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.store import PsycopgGraphLogStore

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from falkordb import FalkorDB
    from persistence.provisioned_postgres import Provisioned

    from ps_service.logging.emitter import LogEmitter


@dataclass(frozen=True, slots=True)
class LiveRestore:
    """What a live `restore_instrument` call needs besides the artifact."""

    gateway: GraphWriteGateway
    audit_event_id: str

    @property
    def open_gateway(self) -> Callable[[], object]:
        """The `open_gateway` argument of `restore_instrument`."""
        return lambda: self.gateway


@contextmanager
def live_restore(
    prov: Provisioned, db: FalkorDB, emitter: LogEmitter | None = None
) -> Generator[LiveRestore]:
    """A real gateway over `prov`'s log and `db`, plus one committed audit row to anchor to."""
    gateway = GraphWriteGateway(
        log_store=PsycopgGraphLogStore(prov.state_config()),
        graph_opener=lambda graph: select_graph(db, graph),
        emitter=emitter,
        key_port=FalkorDBKeyPort(lambda: db),  # a restore is applied by swap (issue #214)
    )
    try:
        yield LiveRestore(gateway, committed_audit_event(prov))
    finally:
        gateway.stop_reconciler()
