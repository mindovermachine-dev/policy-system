"""Shared builders and thread helpers of the swap tests.

Import as `from graph_gateway._swap_support import ...`. The fakes themselves live in `_fakes.py`
(`GatewayRig(swap=True)` wires the in-memory key port of `_swap_fakes.py`).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Literal

from graph_gateway._fakes import GatewayRig, Keys
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.models import GraphLeg, MultiGraphGroup, UpsertNode

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.graph_gateway.models import Primitive

AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
OTHER_EVENT_ID = "9b1e2f3a-4c5d-4e6f-8a7b-1c2d3e4f5a6b"
WAIT_SECONDS = 5.0
BLOCKED_PROBE_SECONDS = 0.3
THREE = ("baseline", "native", "policy_system")

EMPTY_THREE: tuple[Keys, Keys, Keys] = (frozenset(), frozenset(), frozenset())


class Crash(BaseException):
    """Raised by a test hook to stop the gateway mid-apply the way a killed process would."""


def node(node_id: str, **properties: object) -> UpsertNode:
    """A `Capability` upsert."""
    return UpsertNode(label="Capability", id=node_id, properties={"name": node_id, **properties})


def leg(graph: str, *primitives: Primitive, **fields: object) -> GraphLeg:
    """A leg of `graph`; with no primitives it upserts one node named after the graph."""
    return GraphLeg(graph=graph, primitives=primitives or (node(f"{graph}-1"),), **fields)  # pyright: ignore[reportArgumentType]


def swap_set(
    *legs: GraphLeg,
    audit_event_id: str = AUDIT_EVENT_ID,
    apply: Literal["in_place", "swap"] = "swap",
) -> MultiGraphGroup:
    """A set over `legs`, applied by swap unless asked otherwise."""
    return MultiGraphGroup(audit_event_id=audit_event_id, legs=legs, apply=apply)


def three_legs(suffix: str = "") -> tuple[GraphLeg, ...]:
    """Three legs over the three Restore graphs, given out of graph order."""
    return (
        leg("policy_system", node(f"p-1{suffix}"), node(f"p-2{suffix}")),
        leg("native", node(f"n-1{suffix}")),
        leg("baseline", node(f"b-1{suffix}"), node(f"b-2{suffix}"), node(f"b-3{suffix}")),
    )


def new_three() -> tuple[Keys, Keys, Keys]:
    """The node keys `three_legs()` leaves on baseline, native and policy_system."""
    return (
        frozenset({("Capability", "b-1"), ("Capability", "b-2"), ("Capability", "b-3")}),
        frozenset({("Capability", "n-1")}),
        frozenset({("Capability", "p-1"), ("Capability", "p-2")}),
    )


def swap_rig(*, with_port: bool = True, **kwargs: object) -> GatewayRig:
    """A rig whose gateway has the in-memory key port (`rig.key_port`) unless told otherwise."""
    return GatewayRig(swap=with_port, **kwargs)  # pyright: ignore[reportArgumentType]


def live_nodes(rig: GatewayRig, graph: str) -> Keys:
    """The node keys of the live graph `graph`."""
    return frozenset(rig.graphs.open(graph).nodes)


def digests(rig: GatewayRig, graphs: tuple[str, ...] = THREE) -> tuple[str, ...]:
    """The canonical digest of each live graph in `graphs`."""
    return tuple(canonical_digest(rig.graphs.open(graph)) for graph in graphs)


def staged_keys(rig: GatewayRig) -> list[str]:
    """Names of the staged keys the fake key space holds."""
    return rig.key_port.staged_names()


class Gate:
    """Holds the first append that starts until released; later ones pass through."""

    def __init__(self) -> None:
        """Start open for nobody: the first append blocks."""
        self.entered = threading.Event()
        self.release = threading.Event()
        self._first = True
        self._guard = threading.Lock()

    def __call__(self, graph: str) -> None:
        """Block the first caller until `release` is set."""
        del graph
        with self._guard:
            hold, self._first = self._first, False
        if hold:
            self.entered.set()
            assert self.release.wait(WAIT_SECONDS)


class Thread:
    """A daemon thread that keeps what its work returned or raised."""

    def __init__(self, work: Callable[[], object]) -> None:
        """Prepare, but do not start, a thread running `work`."""
        self.result: object = None
        self.error: BaseException | None = None
        self._work = work
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.result = self._work()
        except BaseException as exc:  # noqa: BLE001  (carried to the asserting thread)
            self.error = exc

    def start(self) -> Thread:
        """Start the thread and return it."""
        self._thread.start()
        return self

    def finishes_within(self, seconds: float) -> bool:
        """Wait up to `seconds` and say whether the thread ended."""
        self._thread.join(seconds)
        return not self._thread.is_alive()
