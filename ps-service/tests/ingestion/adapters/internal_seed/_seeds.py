"""Seed builders and a legacy-handle stand-in shared by the internal-seed gateway tests (#208)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from ps_service.ingestion.adapters.internal_seed.models import (
    InternalRegulationSeed,
    SeedEdge,
    SeedNode,
)
from ps_service.ingestion.adapters.internal_seed.persist import (
    ingest_internal_regulatory_instrument,
)

if TYPE_CHECKING:
    from graph_gateway._fakes import GatewayRig, InMemoryGraph

    from ps_service.graph_gateway.edge_identity_backfill import EdgeIdentityBackfillOutcome
    from ps_service.graph_gateway.graph_reader import PersistedElements
    from ps_service.graph_gateway.models import GroupOutcome, MutationGroup
    from ps_service.ingestion.graph_submission import GroupSubmitter

NATIVE_GRAPH = "testreg_native"
BASELINE_GRAPH = "testreg_baseline"


def node(label: str, node_id: str, **properties: str | float) -> SeedNode:
    return SeedNode.model_validate({"label": label, "id": node_id, "properties": properties})


def edge(
    edge_type: str, from_ref: tuple[str, str], to_ref: tuple[str, str], **properties: str
) -> SeedEdge:
    return SeedEdge.model_validate(
        {
            "type": edge_type,
            "from": {"label": from_ref[0], "id": from_ref[1]},
            "to": {"label": to_ref[0], "id": to_ref[1]},
            "properties": properties,
        }
    )


RI = node(
    "RegulatoryInstrument",
    "TESTREG-1.0",
    title="Test Regulation",
    source_type="internal",
    effective_date="2026-01-01",
    version="1.0",
    status="active",
)
RI_REF = ("RegulatoryInstrument", "TESTREG-1.0")
ROLE = ("Role", "role-a")
OBLIGATION = ("Obligation", "obl-a")
REQUIREMENT = ("Requirement", "req-1")
CAPABILITY = ("Capability", "cap-1")
POLICY = ("Policy", "pol-1")
STANDARD = ("Standard", "std-1")
CONTROL = ("Control", "ctrl-1")
PRACTICE_AREA = ("PracticeArea", "pa-1")
RISK_PATH = ("RiskPath", "rp-1")


def full_seed(
    *,
    capability_properties: dict[str, str | float] | None = None,
    role_properties: dict[str, str | float] | None = None,
) -> InternalRegulationSeed:
    """A document with every label: the spine, governance chain and both classification nodes."""
    return InternalRegulationSeed(
        nodes=(
            RI,
            node("Role", "role-a", **(role_properties or {"name": "Role A"})),
            node("Obligation", "obl-a", text="Do A"),
            node("Requirement", "req-1", text="Must A", type="requirement"),
            node("Capability", "cap-1", **(capability_properties or {"name": "Cap One"})),
            node("Policy", "pol-1", title="Access Policy", status="draft"),
            node("Standard", "std-1", title="Access Standard", implementation_status="draft"),
            node(
                "Control",
                "ctrl-1",
                type="detective",
                title="Access Control",
                implementation_status="draft",
            ),
            node("PracticeArea", "pa-1", name="Security", status="active"),
            node("RiskPath", "rp-1", name="Breach", status="active"),
        ),
        edges=(
            edge("DEFINES", RI_REF, ROLE, source_ref="Art. 1"),
            edge("EXPRESSES", RI_REF, REQUIREMENT, source_ref="Art. 2"),
            edge("HAS", ROLE, OBLIGATION),
            edge("SATISFIED_BY", REQUIREMENT, OBLIGATION),
            edge("REQUIRES", OBLIGATION, CAPABILITY),
            edge("GOVERNED_BY", CAPABILITY, POLICY),
            edge("SUPPORTED_BY", POLICY, STANDARD),
            edge("IMPLEMENTED_BY", STANDARD, CONTROL),
            edge("COVERS", PRACTICE_AREA, CAPABILITY),
            edge("OWNS", PRACTICE_AREA, POLICY),
            edge("MITIGATED_BY", RISK_PATH, CAPABILITY),
            edge("VERIFIED_BY", RISK_PATH, CONTROL),
        ),
    )


class RecordingSubmitter:
    """Delegates to the gateway and keeps every group it was given, in submission order."""

    def __init__(self, submitter: GroupSubmitter) -> None:
        self._submitter = submitter
        self.groups: list[MutationGroup] = []

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        self.groups.append(group)
        return self._submitter.submit_group(group)

    def is_caught_up(self, graph: str) -> bool:
        return self._submitter.is_caught_up(graph)

    def stamp_legacy_edge_identity(
        self, graph: str, audit_event_id: str | None
    ) -> EdgeIdentityBackfillOutcome:
        return self._submitter.stamp_legacy_edge_identity(graph, audit_event_id)

    def read_persisted_elements(self, graph: str) -> PersistedElements:
        return self._submitter.read_persisted_elements(graph)


class Run:
    """Ingestions of seeds over one gateway rig; keeps the groups the writer submitted."""

    def __init__(self, rig: GatewayRig) -> None:
        self.rig = rig
        self.audit_event_id = str(uuid.uuid4())
        rig.store.audit_rows.add(self.audit_event_id)
        self.submitter = RecordingSubmitter(rig.gateway)
        self.native_name = NATIVE_GRAPH
        self.baseline_name = BASELINE_GRAPH

    def ingest(self, seed: InternalRegulationSeed) -> None:
        instrument = next(n for n in seed.nodes if n.label == "RegulatoryInstrument")
        short_name = instrument.id.rpartition("-")[0].lower()
        self.native_name, self.baseline_name = f"{short_name}_native", f"{short_name}_baseline"
        ingest_internal_regulatory_instrument(
            seed,
            submitter=self.submitter,
            native_graph_name=self.native_name,
            baseline_graph_name=self.baseline_name,
            audit_event_id=self.audit_event_id,
        )

    @property
    def native(self) -> InMemoryGraph:
        return self.rig.graphs.open(self.native_name)

    @property
    def baseline(self) -> InMemoryGraph:
        return self.rig.graphs.open(self.baseline_name)
