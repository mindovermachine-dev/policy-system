"""The Graph Write Gateway rig, seen from the Domain Mapper stages.

The Domain Mapper submits its baseline writes as gateway groups and only reads the baseline
graph through a handle. `BaselineRig` is the gateway rig (in-memory log and FalkorDB fakes, the
true boundaries) plus that read handle: it answers the one read the stages issue, the batched
existing-node read `MATCH (n:<Label>) WHERE n.id IN $ids RETURN n.id`, from the rig's baseline
graph, and records every query so a test can show the handle never receives a write.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, TypedDict

from graph_gateway._fakes import FakeQueryResult, GatewayRig

from ps_service.graph_gateway.models import MutationGroup, NodeRef, UpsertEdge, UpsertNode

if TYPE_CHECKING:
    from graph_gateway._fakes import InMemoryGraph

    from ps_service.domain_mapper.falkordb_client import GraphHandle
    from ps_service.graph_gateway.models import GraphLogEntry, GraphLogGroup, Primitive
    from ps_service.ingestion.graph_submission import GroupSubmitter

BASELINE_GRAPH_NAME = "cra_baseline"

_EXISTING_NODE_READ = re.compile(
    r"^MATCH \(n:(?P<label>[A-Za-z_]+)\) WHERE n\.id IN \$ids RETURN n\.id$"
)


class GatewayKeywords(TypedDict):
    """The keywords that hand a Domain Mapper stage the gateway and the graph to write."""

    submitter: GroupSubmitter
    baseline_graph_name: str
    audit_event_id: str


class StageGatewayKeywords(TypedDict):
    """The keywords a Domain Mapper stage takes for its baseline graph (read) and the gateway."""

    baseline_graph: GraphHandle
    submitter: GroupSubmitter
    baseline_graph_name: str
    audit_event_id: str


class BaselineReadHandle:
    """A read-only `GraphHandle` over the rig's baseline graph.

    It answers the batched existing-node read and the derivation stage's Requirements-by-Role read
    from the graph the gateway has applied; any other query (a write) is a failure.
    """

    def __init__(self, graph: InMemoryGraph) -> None:
        self._graph = graph
        self.queries: list[str] = []
        """Every query received, in order."""
        self.error: Exception | None = None
        """When set, every query raises it (FalkorDB down for the read)."""

    def query(self, q: str, params: dict[str, object] | None = None) -> FakeQueryResult:
        """Answer the existing-node read from the graph's nodes."""
        self.queries.append(q)
        if self.error is not None:
            raise self.error
        if "OPTIONAL MATCH (rl:Role" in q:
            return FakeQueryResult(self._requirement_rows(str((params or {})["rid"])))
        match = _EXISTING_NODE_READ.match(q)
        if match is None:
            message = f"the baseline read handle answers only the existing-node read, got: {q}"
            raise AssertionError(message)
        ids = (params or {}).get("ids")
        assert isinstance(ids, list)
        label = match.group("label")
        found = [
            [node_id]
            for node_id in (str(i) for i in ids)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            if (label, node_id) in self._graph.nodes
        ]
        return FakeQueryResult(list(found))  # pyright: ignore[reportUnknownArgumentType]

    def _requirement_rows(self, instrument_id: str) -> list[object]:
        """`req.id, req.text, req.role_id, rl.id, rl.name` for what `instrument_id` EXPRESSES."""
        rows: list[object] = []
        for edge_type, source_label, source_id, target_label, target_id, _ in self._graph.edges:
            if (edge_type, source_label, source_id, target_label) != (
                "EXPRESSES",
                "RegulatoryInstrument",
                instrument_id,
                "Requirement",
            ):
                continue
            requirement = self._graph.nodes[("Requirement", target_id)]
            role_id = requirement.get("role_id")
            role = self._graph.nodes.get(("Role", str(role_id)))
            rows.append(
                [
                    target_id,
                    requirement.get("text"),
                    role_id,
                    None if role is None else role_id,
                    None if role is None else role.get("name"),
                ]
            )
        return rows


class BaselineRig(GatewayRig):
    """A gateway rig that also hands out the `{short}_baseline` read handle and an audit id."""

    def __init__(self) -> None:
        super().__init__()
        self.baseline_graph_name = BASELINE_GRAPH_NAME
        self.audit_event_id = str(uuid.uuid4())
        self.store.audit_rows.add(self.audit_event_id)
        self.baseline: InMemoryGraph = self.graphs.open(BASELINE_GRAPH_NAME)
        """The rig's baseline graph: what the gateway has applied."""
        self.read_graph = BaselineReadHandle(self.baseline)

    @property
    def submitter(self) -> GroupSubmitter:
        """The gateway, as the stages receive it."""
        return self.gateway

    def gateway_keywords(self) -> GatewayKeywords:
        """The gateway keywords alone: a stage that reads no baseline graph (extraction)."""
        return {
            "submitter": self.gateway,
            "baseline_graph_name": self.baseline_graph_name,
            "audit_event_id": self.audit_event_id,
        }

    def stage_kwargs(self) -> StageGatewayKeywords:
        """The keywords the orchestrator hands a Domain Mapper stage, plus its read handle."""
        return {
            "baseline_graph": self.read_graph,
            "submitter": self.gateway,
            "baseline_graph_name": self.baseline_graph_name,
            "audit_event_id": self.audit_event_id,
        }

    @property
    def groups(self) -> list[GraphLogGroup]:
        """Every group logged on the baseline graph, oldest first."""
        return [g for g in self.store.groups if g.graph == self.baseline_graph_name]

    def logged(self) -> list[GraphLogEntry]:
        """Every entry logged on the baseline graph, in position order."""
        return list(self.store.entries.get(self.baseline_graph_name, []))

    def entries_named(self, name: str) -> list[GraphLogEntry]:
        """The logged entries whose label or relationship type is `name`, in position order."""
        return [entry for entry in self.logged() if entry.name == name]


def seed_extraction_output(
    rig: BaselineRig,
    instrument_id: str,
    roles: dict[str, str],
    requirements: list[tuple[str, str, str]],
) -> None:
    """Leave in the rig's baseline graph what extraction leaves for derivation to read.

    `roles` maps Role id to name; `requirements` lists `(id, text, role_id)`. Submitted as one
    group through the gateway, as extraction does, so it is the first group of the rig's log.
    """
    primitives: list[Primitive] = [UpsertNode(label="RegulatoryInstrument", id=instrument_id)]
    primitives += [
        UpsertNode(label="Role", id=role_id, properties={"name": name})
        for role_id, name in roles.items()
    ]
    primitives += [
        UpsertNode(label="Requirement", id=req_id, properties={"text": text, "role_id": role_id})
        for req_id, text, role_id in requirements
    ]
    primitives += [
        UpsertEdge(
            type="EXPRESSES",
            identity=f"EXPRESSES:{instrument_id}->{req_id}",
            source=NodeRef(label="RegulatoryInstrument", id=instrument_id),
            target=NodeRef(label="Requirement", id=req_id),
            properties={"source_ref": "Art. 1"},
        )
        for req_id, _text, _role_id in requirements
    ]
    rig.gateway.submit_group(
        MutationGroup(
            graph=rig.baseline_graph_name,
            audit_event_id=rig.audit_event_id,
            primitives=tuple(primitives),
        )
    )


def entry_properties(entry: GraphLogEntry) -> dict[str, object]:
    """The properties a logged node or edge entry writes."""
    properties = entry.content["properties"]
    assert isinstance(properties, dict)
    return {str(key): value for key, value in properties.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
