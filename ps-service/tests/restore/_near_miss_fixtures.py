"""An artifact whose Capabilities land just below the threshold of Capabilities already held.

Two Capabilities are seeded into `policy_system`; the artifact brings two more, each scoring 0.8
against one seeded Capability, so a restore at `THRESHOLD` (0.85) finds exactly two near misses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from restore._fixtures import build_restore_artifact, small_graphs

if TYPE_CHECKING:
    from ps_service.restore.models import RestoreArtifact
    from restore._gateway_fakes import RestoreRig

INSTRUMENT_ID = "NM339-1.0"
POLICY_SYSTEM = "policy_system"

# id -> (embedding, name)
EXISTING = {
    "cap_ex_a": ([1.0, 0.0, 0.0, 0.0], "Keep Records"),
    "cap_ex_b": ([0.0, 1.0, 0.0, 0.0], "Patch Fast"),
}
# id -> (embedding, name, nearest existing id)
INCOMING = {
    "cap_in_a": ([0.8, 0.6, 0.0, 0.0], "Retain Records", "cap_ex_a"),
    "cap_in_b": ([0.0, 0.8, 0.6, 0.0], "Patch Quickly", "cap_ex_b"),
}


def near_miss_artifact() -> RestoreArtifact:
    """The artifact: the small external instrument plus the two near-miss Capabilities."""
    native, baseline = small_graphs(instrument_id=INSTRUMENT_ID)
    capabilities = tuple(
        SerializedNode(
            "Capability", {"id": cap_id, "name": name, "confidence": 0.8, "embedding": embedding}
        )
        for cap_id, (embedding, name, _) in INCOMING.items()
    )
    requires = tuple(
        SerializedEdge("REQUIRES", "Obligation", "obl_1", "Capability", cap_id, {})
        for cap_id in INCOMING
    )
    graph = SerializedGraph(
        nodes=(*baseline.nodes, *capabilities), edges=(*baseline.edges, *requires)
    )
    return build_restore_artifact(
        instrument_id=INSTRUMENT_ID,
        short_name="NM339",
        native_graph=native,
        baseline_graph=graph,
    )


def seed_existing(rig: RestoreRig) -> None:
    """Put the two Capabilities the incoming ones nearly match into `policy_system`."""
    for cap_id, (embedding, name) in EXISTING.items():
        rig.policy_system.nodes[("Capability", cap_id)] = {
            "id": cap_id,
            "name": name,
            "embedding": embedding,
            "status": "active",
        }


def reviews(rig: RestoreRig) -> dict[str, dict[str, object]]:
    """The `PendingReview` nodes `policy_system` holds now, by id (the swap replaces the graph)."""
    return {
        key[1]: props
        for key, props in rig.graph(POLICY_SYSTEM).nodes.items()
        if key[0] == "PendingReview"
    }
