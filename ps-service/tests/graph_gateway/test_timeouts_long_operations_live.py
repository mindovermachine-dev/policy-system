"""`falkordb_live`: whole-graph copy and a digest scan complete inside the default socket timeout.

The connection is built from `load_config()` defaults (5 s connect, 30 s socket), the way the
service builds it, over the CRA baseline (`curated-content/CRA-1.0/baseline.json`). Measured
durations are printed (`pytest -s`).

Run with `uv run pytest -s -m falkordb_live <this file>` against the FalkorDB named by
`PS_TEST_FALKORDB_HOST` and `PS_TEST_FALKORDB_PORT` (default 127.0.0.1:6379).
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from graph_gateway._fakes import InMemoryGraphLogStore
from graph_gateway.live_endpoints import falkordb_endpoint
from ps_service.config import load_config
from ps_service.export.serialize import parse_serialized_graph_json
from ps_service.graph_gateway.connection import select_graph
from ps_service.graph_gateway.default_gateway import FalkorDBGraphOpener, FalkorDBKeyPort
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.models import (
    GraphLeg,
    MultiGraphGroup,
    MutationGroup,
    UpsertNode,
)
from ps_service.restore.leg_primitives import leg_primitives

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.config import ServiceConfig

pytestmark = pytest.mark.falkordb_live

_BASELINE = Path(__file__).resolve().parents[3] / "curated-content" / "CRA-1.0" / "baseline.json"
_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GROUP_SIZE = 500


@dataclasses.dataclass
class _Rig:
    """A gateway over a default-timeout opener, and the CRA graph it seeded."""

    config: ServiceConfig
    opener: FalkorDBGraphOpener
    gateway: GraphWriteGateway
    graph: str
    stem: str

    def graph_keys(self) -> list[str]:
        return [name for name in self.opener.database().list_graphs() if name.startswith(self.stem)]


@pytest.fixture
def rig() -> Iterator[_Rig]:
    host, port = falkordb_endpoint()
    config = dataclasses.replace(load_config(), falkordb_host=host, falkordb_port=port)
    opener = FalkorDBGraphOpener(config)
    gateway = GraphWriteGateway(
        log_store=InMemoryGraphLogStore(),
        graph_opener=opener,
        key_port=FalkorDBKeyPort(opener.database),
    )
    stem = f"long_ops_{uuid.uuid4().hex[:8]}"
    graph = f"{stem}_cra"
    primitives = leg_primitives(parse_serialized_graph_json(_BASELINE.read_bytes()))
    for start in range(0, len(primitives), _GROUP_SIZE):
        gateway.submit_group(
            MutationGroup(
                graph=graph,
                audit_event_id=_AUDIT_EVENT_ID,
                primitives=primitives[start : start + _GROUP_SIZE],
            )
        )
    holder = _Rig(config=config, opener=opener, gateway=gateway, graph=graph, stem=stem)
    yield holder
    for name in holder.graph_keys():
        opener.database().select_graph(name).delete()


def test_the_defaults_are_the_documented_five_and_thirty_seconds(rig: _Rig) -> None:
    assert rig.config.falkordb_connect_timeout_seconds == 5.0
    assert rig.config.falkordb_socket_timeout_seconds == 30.0


def test_a_graph_copy_of_the_cra_baseline_completes_at_the_default_socket_timeout(
    rig: _Rig,
) -> None:
    staged = f"{rig.stem}_copy"
    port = FalkorDBKeyPort(rig.opener.database)

    started = time.perf_counter()
    port.copy(rig.graph, staged)
    seconds = time.perf_counter() - started

    print(f"\n[timeouts] GRAPH.COPY of CRA {seconds:.2f}s (socket {30:.0f}s)")  # noqa: T201
    assert seconds < rig.config.falkordb_socket_timeout_seconds
    assert canonical_digest(select_graph(rig.opener.database(), staged)) == canonical_digest(
        select_graph(rig.opener.database(), rig.graph)
    )


def test_a_swap_over_the_cra_baseline_copies_and_completes_at_the_defaults(rig: _Rig) -> None:
    before = canonical_digest(select_graph(rig.opener.database(), rig.graph))
    other = f"{rig.stem}_other"
    rig.gateway.submit_group(
        MutationGroup(
            graph=other,
            audit_event_id=_AUDIT_EVENT_ID,
            primitives=(UpsertNode(label="Capability", id="seed"),),
        )
    )
    group = MultiGraphGroup(
        audit_event_id=_AUDIT_EVENT_ID,
        legs=tuple(
            GraphLeg(graph=graph, primitives=(UpsertNode(label="Capability", id="added-by-swap"),))
            for graph in sorted((rig.graph, other))
        ),
        apply="swap",
    )

    started = time.perf_counter()
    outcome = rig.gateway.submit_multi_graph_group(group)
    seconds = time.perf_counter() - started

    print(f"\n[timeouts] swap set over CRA {seconds:.2f}s (socket {30:.0f}s)")  # noqa: T201
    assert outcome.status == "applied"
    assert seconds < rig.config.falkordb_socket_timeout_seconds
    assert sorted(rig.graph_keys()) == sorted([rig.graph, other])  # no staged key is left
    assert canonical_digest(select_graph(rig.opener.database(), rig.graph)) != before


def test_a_digest_scan_of_the_cra_baseline_completes_at_the_default_socket_timeout(
    rig: _Rig,
) -> None:
    started = time.perf_counter()
    digest = canonical_digest(select_graph(rig.opener.database(), rig.graph))
    seconds = time.perf_counter() - started

    print(f"\n[timeouts] digest scan of CRA {seconds:.2f}s (socket {30:.0f}s)")  # noqa: T201
    assert digest
    assert seconds < rig.config.falkordb_socket_timeout_seconds
