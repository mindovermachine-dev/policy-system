"""Shared assertions for "a writer left a head checkpoint" (issue #215, S4; AC-BI-009).

A head checkpoint is the stored digest at a graph's last log position. Writers request one on their
terminal group; each flow's test asserts it through these helpers on the `GatewayRig` it ran on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.graph_gateway.digest import canonical_digest

if TYPE_CHECKING:
    from collections.abc import Iterable

    from graph_gateway._fakes import GatewayRig


def head_checkpoint_digest(rig: GatewayRig, graph: str) -> str | None:
    """The checkpointed digest at `graph`'s last log position, `None` when there is none."""
    position = rig.store.last_position(graph)
    checkpoint = rig.store.read_digest_checkpoint(graph, position)
    return None if checkpoint is None else checkpoint.canonical_digest


def assert_head_checkpoint(rig: GatewayRig, graph: str) -> None:
    """`graph` has a checkpoint at its head and it equals the live canonical digest."""
    stored = head_checkpoint_digest(rig, graph)
    assert stored is not None, (
        f"{graph}: no checkpoint at position {rig.store.last_position(graph)}"
    )
    assert stored == canonical_digest(rig.graphs.open(graph)), graph


def assert_head_checkpoints(rig: GatewayRig, graphs: Iterable[str] | None = None) -> None:
    """Every named graph (default: every logged graph) has a matching head checkpoint."""
    names = tuple(graphs) if graphs is not None else rig.store.logged_graphs()
    assert names, "nothing was logged"
    for graph in names:
        assert_head_checkpoint(rig, graph)


def assert_no_checkpoint(rig: GatewayRig, graph: str | None = None) -> None:
    """No checkpoint exists (for `graph`, or at all)."""
    found = [key for key in rig.store.checkpoints if graph is None or key[0] == graph]
    assert found == [], found
