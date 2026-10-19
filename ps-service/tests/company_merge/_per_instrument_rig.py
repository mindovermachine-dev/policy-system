"""Seeding helpers for the per-instrument review-kind tests (live, offline and vertical).

A node of a per-instrument kind is anchored by a `DEFINES` edge from its instrument. Seeding puts
the node and that edge in the rig's `policy_system` graph, as a merge of the instrument would.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from company_merge._gateway_fakes import MergeRig

INSTRUMENT_A = "instrument_a"
INSTRUMENT_B = "instrument_b"


def seed_defined_node(
    rig: MergeRig,
    *,
    label: str,
    node_id: str,
    name: str,
    instrument_id: str | None,
    embedding: tuple[float, ...] | None = None,
) -> None:
    """Store a `label` node in `policy_system`; `instrument_id` `DEFINES` it unless it is None."""
    properties: dict[str, object] = {"id": node_id, "name": name}
    if embedding is not None:
        properties["embedding"] = list(embedding)
    rig.policy_system.nodes[(label, node_id)] = properties
    if instrument_id is not None:
        key = ("DEFINES", "RegulatoryInstrument", instrument_id, label, node_id, "")
        rig.policy_system.edges[key] = {}
