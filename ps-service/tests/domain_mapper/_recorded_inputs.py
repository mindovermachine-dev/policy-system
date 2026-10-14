"""Recorded baseline artifacts as the inputs of the Domain Mapper's two writers.

`recorded_inputs` reads a baseline artifact in the curated `baseline.json` shape (the committed
trimmed CRA subset, or the whole curated artifact) and returns the Role/Requirement/Obligation/
Capability nodes and edges as the `domain_mapper.models` types the writers take. Shared by the fast
equivalence test and its live twin.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ps_service.domain_mapper.models import (
    CapabilityNode,
    CapabilityRequiresEdge,
    ObligationHasEdge,
    ObligationNode,
    RequirementExpressesEdge,
    RequirementNode,
    RequirementSatisfiedByEdge,
    RoleDefinesEdge,
    RoleNode,
)

_TESTS = Path(__file__).resolve().parents[1]
SUBSET_FIXTURE = _TESTS / "fixtures" / "domain-mapper" / "recorded-extraction-cra-subset.json"
FULL_ARTIFACT = _TESTS.parents[1] / "curated-content" / "CRA-1.0" / "baseline.json"
# The writer is never given these: `id` is the node identity, `embedding` is the Company Merge's.
_NOT_WRITER_PROPERTIES = frozenset({"id", "embedding"})


def recorded_inputs(path: Path) -> Inputs:
    """The writer's inputs read back from a recorded baseline artifact (raw `baseline.json` shape).

    Role, Requirement, Obligation and Capability nodes become the matching `domain_mapper.models`
    nodes (`id` split out of the properties, embeddings dropped) and the five edge types their edge
    models; the artifact's RegulatoryInstrument node is not a writer input (it is a property bag).
    """
    artifact = cast("dict[str, list[dict[str, object]]]", json.loads(path.read_text("utf-8")))

    def nodes(label: str) -> list[tuple[str, dict[str, str | float]]]:
        found: list[tuple[str, dict[str, str | float]]] = []
        for node in artifact["nodes"]:
            if node["label"] == label:
                properties = cast("dict[str, object]", node["properties"])
                writer_properties = cast(
                    "dict[str, str | float]",
                    {k: v for k, v in properties.items() if k not in _NOT_WRITER_PROPERTIES},
                )
                found.append((cast("str", properties["id"]), writer_properties))
        return found

    def edges(relationship: str) -> list[dict[str, object]]:
        return [e for e in artifact["edges"] if e["relationship_type"] == relationship]

    return Inputs(
        role_nodes=tuple(RoleNode(i, p) for i, p in nodes("Role")),
        role_edges=tuple(
            RoleDefinesEdge(
                cast("str", e["target_id"]),
                cast("str", cast("dict[str, object]", e["properties"])["source_ref"]),
            )
            for e in edges("DEFINES")
        ),
        requirement_nodes=tuple(RequirementNode(i, p) for i, p in nodes("Requirement")),
        requirement_edges=tuple(
            RequirementExpressesEdge(
                cast("str", e["target_id"]),
                cast("str", cast("dict[str, object]", e["properties"])["source_ref"]),
            )
            for e in edges("EXPRESSES")
        ),
        obligation_nodes=tuple(ObligationNode(i, p) for i, p in nodes("Obligation")),
        has_edges=tuple(
            ObligationHasEdge(cast("str", e["source_id"]), cast("str", e["target_id"]))
            for e in edges("HAS")
        ),
        satisfied_by_edges=tuple(
            RequirementSatisfiedByEdge(cast("str", e["source_id"]), cast("str", e["target_id"]))
            for e in edges("SATISFIED_BY")
        ),
        capability_nodes=tuple(CapabilityNode(i, p) for i, p in nodes("Capability")),
        requires_edges=tuple(
            CapabilityRequiresEdge(cast("str", e["source_id"]), cast("str", e["target_id"]))
            for e in edges("REQUIRES")
        ),
    )


@dataclass(frozen=True)
class Inputs:
    """What the two writers are given: the output of extraction and derivation's pure steps."""

    role_nodes: tuple[RoleNode, ...] = ()
    role_edges: tuple[RoleDefinesEdge, ...] = ()
    requirement_nodes: tuple[RequirementNode, ...] = ()
    requirement_edges: tuple[RequirementExpressesEdge, ...] = ()
    obligation_nodes: tuple[ObligationNode, ...] = ()
    has_edges: tuple[ObligationHasEdge, ...] = ()
    satisfied_by_edges: tuple[RequirementSatisfiedByEdge, ...] = ()
    capability_nodes: tuple[CapabilityNode, ...] = ()
    requires_edges: tuple[CapabilityRequiresEdge, ...] = ()
