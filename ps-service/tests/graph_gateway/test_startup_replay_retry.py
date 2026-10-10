"""`run_startup_replay` keeps the startup replay going through an infrastructure outage (#207 S17b).

A startup replay that meets Postgres or FalkorDB down must not give up: the pod would sit at
`not_ready` for good. `run_startup_replay` retries `startup_replay` with a growing, capped wait
(each retry resumes from the graph's progress record), ends early when a stop is requested, and
does not retry anything that is not an infrastructure outage.
"""

from __future__ import annotations

import threading
import time

import pytest
import redis.exceptions

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.errors import GraphLogUnavailableError
from ps_service.graph_gateway.gateway import GatewaySettings
from ps_service.graph_gateway.models import MutationGroup, StartupReplayReport, UpsertNode

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_DOWN = redis.exceptions.ConnectionError("falkordb is down")
_FAST = GatewaySettings(
    replay_page_size=2,
    batch_size=1,
    initial_backoff_seconds=0.001,
    startup_replay_max_backoff_seconds=0.01,
)


def _lost_graph(settings: GatewaySettings) -> tuple[GatewayRig, str]:
    """A rig whose graph was logged (five entries) and then lost; returns it and the digest."""
    rig = GatewayRig(settings=settings)
    for number in range(5):
        rig.gateway.submit_group(
            MutationGroup(
                graph=_GRAPH,
                audit_event_id=_AUDIT_EVENT_ID,
                primitives=(UpsertNode(label="Capability", id=f"cap-{number}", properties={}),),
            )
        )
    digest = canonical_digest(rig.graphs.open(_GRAPH))
    rig.graphs.open(_GRAPH).flush()
    return rig, digest


def test_a_falkordb_outage_during_the_startup_replay_is_retried_until_it_heals() -> None:
    rig, expected = _lost_graph(_FAST)
    graph = rig.graphs.open(_GRAPH)
    graph.fail_on_write(_DOWN, times=3)  # three writes fail, the fourth attempt gets through
    gateway = rig.restart()

    report = gateway.run_startup_replay()

    assert report.replayed + report.resumed == (_GRAPH,)
    assert canonical_digest(graph) == expected
    assert gateway.gated_graphs() == ()


def test_a_log_outage_during_the_startup_replay_is_retried_until_it_heals() -> None:
    rig, expected = _lost_graph(_FAST)
    rig.store.fail_reads(times=4)
    gateway = rig.restart()

    report = gateway.run_startup_replay()

    assert report.replayed + report.resumed == (_GRAPH,)
    assert canonical_digest(rig.graphs.open(_GRAPH)) == expected


def test_the_retry_wait_grows_and_is_capped() -> None:
    rig, _ = _lost_graph(
        GatewaySettings(
            initial_backoff_seconds=0.001,
            backoff_multiplier=2.0,
            startup_replay_max_backoff_seconds=0.004,
        )
    )
    rig.store.fail_reads(times=6)
    gateway = rig.restart()
    waited: list[float] = []

    report = gateway.run_startup_replay(wait=lambda delay: waited.append(delay) or False)

    assert report.replayed == (_GRAPH,)
    assert waited == [0.001, 0.002, 0.004, 0.004, 0.004, 0.004]


def test_a_stop_request_ends_the_retrying_with_the_last_infrastructure_error() -> None:
    rig, _ = _lost_graph(
        GatewaySettings(initial_backoff_seconds=30.0, startup_replay_max_backoff_seconds=30.0)
    )
    rig.store.fail_reads()  # Postgres never comes back
    gateway = rig.restart()
    threading.Timer(0.2, gateway.stop_replay).start()
    started = time.monotonic()

    with pytest.raises(GraphLogUnavailableError):
        gateway.run_startup_replay()

    assert time.monotonic() - started < 5.0  # woke from its 30 s wait when the stop came


def test_an_error_that_is_not_an_outage_is_not_retried() -> None:
    rig, _ = _lost_graph(_FAST)
    rig.graphs.open(_GRAPH).fail_on_write(redis.exceptions.ResponseError("bad query"))
    gateway = rig.restart()
    waited: list[float] = []

    with pytest.raises(redis.exceptions.ResponseError):
        gateway.run_startup_replay(wait=lambda delay: waited.append(delay) or False)

    assert waited == []


def test_a_clean_startup_replay_waits_for_nothing() -> None:
    rig, _ = _lost_graph(_FAST)
    waited: list[float] = []

    report = rig.restart().run_startup_replay(wait=lambda delay: waited.append(delay) or False)

    assert report == StartupReplayReport(replayed=(_GRAPH,))
    assert waited == []
