"""The background reconciler applies committed-but-pending entries on recovery (issue #206, S12).

The reconciler starts lazily when a graph first goes pending, runs on a daemon thread, backs off
with a capped schedule, retires when nothing is pending and stops promptly on request. Its waits
are injected (`SteppedWait`) so the tests step through passes deterministically; only the
"stop interrupts the wait" test uses the real `Event.wait`.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from graph_gateway._fakes import GatewayRig, SteppedWait
from ps_service.graph_gateway.errors import GraphApplyError
from ps_service.graph_gateway.gateway import GatewaySettings
from ps_service.graph_gateway.models import MutationGroup, UpsertNode

if TYPE_CHECKING:
    from collections.abc import Iterator

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_DOWN = redis.exceptions.ConnectionError("Error 111 connecting to 10.1.2.3:6379.")
_WAIT_SECONDS = 5.0


def _group(node_id: str = "cap-1") -> MutationGroup:
    return MutationGroup(
        graph=_GRAPH,
        audit_event_id=_AUDIT_EVENT_ID,
        primitives=(UpsertNode(label="Capability", id=node_id, properties={"name": node_id}),),
    )


def _wait_until_reconciler_retires(rig: GatewayRig) -> None:
    """Poll (bounded) until the reconciler found nothing pending and ended its thread."""
    deadline = time.monotonic() + _WAIT_SECONDS
    while rig.gateway.is_reconciling and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not rig.gateway.is_reconciling


@pytest.fixture
def stepped() -> Iterator[SteppedWait]:
    wait = SteppedWait(timeout=_WAIT_SECONDS)
    yield wait
    wait.stopped.set()
    wait.resume()
    wait.resume()


def test_reconciler_applies_pending_entry_once_graph_recovers(stepped: SteppedWait) -> None:
    rig = GatewayRig(reconciler_wait=stepped)
    graph = rig.graphs.open(_GRAPH)
    graph.fail_on_write(_DOWN)
    assert rig.gateway.submit_group(_group()).status == "committed_apply_pending"
    stepped.await_wait()  # the reconciler backs off before its first pass
    assert rig.store.read_applied_position(_GRAPH) == 0
    graph.heal()

    stepped.resume()

    assert rig.store.marker_advanced.wait(_WAIT_SECONDS)
    _wait_until_reconciler_retires(rig)
    assert rig.store.read_applied_position(_GRAPH) == 1
    assert ("Capability", "cap-1") in graph.nodes
    assert rig.gateway.is_caught_up(_GRAPH) is True


def test_reconciler_keeps_entries_pending_while_graph_stays_down(stepped: SteppedWait) -> None:
    rig = GatewayRig(reconciler_wait=stepped)
    rig.graphs.open(_GRAPH).fail_on_write(_DOWN)
    rig.gateway.submit_group(_group())

    stepped.await_wait()
    stepped.resume()
    stepped.await_wait()
    stepped.resume()
    stepped.await_wait()

    assert stepped.delays[:3] == [0.2, 0.4, 0.8]
    assert rig.store.read_applied_position(_GRAPH) == 0
    assert rig.store.last_position(_GRAPH) == 1
    assert rig.gateway.is_caught_up(_GRAPH) is False
    assert rig.gateway.is_reconciling


def test_reconciler_backoff_is_capped(stepped: SteppedWait) -> None:
    settings = GatewaySettings(initial_backoff_seconds=10.0, reconciler_max_backoff_seconds=15.0)
    rig = GatewayRig(settings=settings, reconciler_wait=stepped)
    rig.graphs.open(_GRAPH).fail_on_write(_DOWN)
    rig.gateway.submit_group(_group())

    for _ in range(3):
        stepped.await_wait()
        stepped.resume()
    stepped.await_wait()

    assert stepped.delays[:4] == [10.0, 15.0, 15.0, 15.0]


def test_reconciler_not_started_when_nothing_pending() -> None:
    rig = GatewayRig()

    assert rig.gateway.submit_group(_group()).status == "applied"

    assert not rig.gateway.is_reconciling


def test_permanent_apply_error_after_commit_is_not_reconciled() -> None:
    rig = GatewayRig()
    rig.graphs.open(_GRAPH).fail_on_write(redis.exceptions.ResponseError("refused"))

    with pytest.raises(GraphApplyError):
        rig.gateway.submit_group(_group())

    assert not rig.gateway.is_reconciling
    assert rig.gateway.is_caught_up(_GRAPH) is False


def test_reconciler_drops_a_graph_the_graph_store_starts_refusing(stepped: SteppedWait) -> None:
    rig = GatewayRig(reconciler_wait=stepped)
    graph = rig.graphs.open(_GRAPH)
    graph.fail_on_write(_DOWN)
    rig.gateway.submit_group(_group())
    stepped.await_wait()
    graph.fail_on_write(redis.exceptions.ResponseError("refused"))

    stepped.resume()

    _wait_until_reconciler_retires(rig)
    assert rig.store.read_applied_position(_GRAPH) == 0
    assert stepped.delays == [0.2]  # no second backoff: the graph left the reconciler's set


def test_reconciler_stop_interrupts_backoff_wait() -> None:
    settings = GatewaySettings(initial_backoff_seconds=60.0, reconciler_max_backoff_seconds=60.0)
    rig = GatewayRig(settings=settings, real_reconciler_wait=True)  # a real 60 s backoff
    rig.graphs.open(_GRAPH).fail_on_write(_DOWN)
    rig.gateway.submit_group(_group())
    assert rig.gateway.is_reconciling

    stopped = rig.gateway.stop_reconciler(timeout=_WAIT_SECONDS)

    assert stopped is True
    assert not rig.gateway.is_reconciling


def test_a_reconciler_that_does_not_stop_is_abandoned_and_reported(stepped: SteppedWait) -> None:
    rig = GatewayRig(reconciler_wait=stepped)
    rig.graphs.open(_GRAPH).fail_on_write(_DOWN)
    rig.gateway.submit_group(_group())
    stepped.await_wait()  # parked inside a wait that ignores the stop request

    stopped = rig.gateway.stop_reconciler(timeout=0.05)

    assert stopped is False
    assert rig.gateway.is_reconciling  # abandoned: a daemon thread, it cannot hold up shutdown
